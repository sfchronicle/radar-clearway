"""The Cloudflare-aware HTTP session.

A :class:`CloudflareSession` fetches URLs with curl_cffi's Chrome TLS
impersonation and, when a request is met with a Cloudflare 403 JS challenge,
solves it once via a browser :class:`~clearway.harvest.Harvester`, caches the
resulting ``cf_clearance`` *per host*, and replays it on later requests to that
host — so a batch of PDF downloads pays the browser cost at most once per host.

Transient failures (network errors and Cloudflare-style 5xx incl. 522) are
retried with capped exponential backoff.  On non-retryable HTTP errors and after
retries are exhausted, the standard library's :class:`urllib.error.HTTPError` /
:class:`~urllib.error.URLError` is raised, so callers that already catch those
keep working.
"""

from __future__ import annotations

import logging
import time
from io import BytesIO
from urllib.error import HTTPError, URLError

from curl_cffi import requests as cffi_requests
from curl_cffi.requests.errors import RequestsError

import atexit

from .config import SiteConfig
from .harvest import (
    BrowserFetcher,
    BrowserResult,
    Harvester,
    PlaywrightFetcher,
    PlaywrightHarvester,
)
from .urls import host_of, normalize_request_url, origin_of

log = logging.getLogger("clearway.session")


def _parse_cookie(header: str) -> list[tuple[str, str]]:
    """Split a ``a=1; b=2`` cookie header into ordered (name, value) pairs."""
    pairs: list[tuple[str, str]] = []
    for part in header.split(";"):
        part = part.strip()
        if not part:
            continue
        name, sep, value = part.partition("=")
        if sep:
            pairs.append((name.strip(), value.strip()))
    return pairs


def _merge_cookies(base: str, override: str) -> str:
    """Merge two cookie headers by name; ``override`` wins on a name collision.

    Prevents two values for the same cookie (e.g. a stale configured
    ``cf_clearance`` plus a freshly harvested one) ending up in a single header,
    which Cloudflare rejects.  Order follows first appearance; an overridden
    cookie keeps its original position with the new value.
    """
    merged: dict[str, str] = {}
    for name, value in _parse_cookie(base) + _parse_cookie(override):
        merged[name] = value  # later (override) assignment wins
    return "; ".join(f"{name}={value}" for name, value in merged.items())


class CloudflareSession:
    """Fetches URLs for one site, transparently handling Cloudflare challenges.

    Args:
        config:    per-site settings.  Defaults to a bare :class:`SiteConfig`
                   (default Chrome profile, no cookie/proxy).
        harvester: browser backend used to solve challenges.  Defaults to
                   :class:`~clearway.harvest.PlaywrightHarvester`; pass your own
                   (nodriver, FlareSolverr, …) to swap the browser without
                   touching this class.
        fetcher:   browser backend that DOWNLOADS a URL when even a harvested
                   cf_clearance is rejected (strict sites like nycourts reject
                   the cookie handed off to curl_cffi on a fingerprint mismatch).
                   Defaults to :class:`~clearway.harvest.PlaywrightFetcher`.
        browser_fallback: set False to disable the browser-fetch fallback (and
                   not create a default fetcher).  Ignored if ``fetcher`` is set.
    """

    def __init__(
        self,
        config: SiteConfig | None = None,
        *,
        harvester: Harvester | None = None,
        fetcher: BrowserFetcher | None = None,
        browser_fallback: bool = True,
    ):
        self.config = config or SiteConfig()
        self.harvester: Harvester = harvester or PlaywrightHarvester()
        if fetcher is not None:
            self.fetcher: BrowserFetcher | None = fetcher
        elif browser_fallback:
            self.fetcher = PlaywrightFetcher()
        else:
            self.fetcher = None
        # host -> harvested cookie string (cf_clearance etc.).  cf_clearance is
        # host-scoped, so it is never shared across hosts.
        self._cf_cookies: dict[str, str] = {}
        # hosts proven to need the browser fetch (cookie hand-off rejected), so
        # later requests skip the doomed curl_cffi attempt and go straight there.
        self._browser_only: set[str] = set()
        if self.fetcher is not None:
            atexit.register(self.close)

    def close(self) -> None:
        """Release the browser fetcher, if any.  Safe to call more than once."""
        if self.fetcher is not None:
            self.fetcher.close()

    def __enter__(self) -> "CloudflareSession":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- headers -----------------------------------------------------------

    def _headers(self, url: str) -> dict[str, str]:
        """User-Agent / Cookie / Referer for a request to *url*.

        Only User-Agent is forced (to match the harvester's UA); curl_cffi's
        impersonate keeps supplying the matching sec-ch-ua client hints.
        """
        cfg = self.config
        h: dict[str, str] = {"User-Agent": cfg.profile.user_agent}

        harvested = self._cf_cookies.get(host_of(url), "")
        # Merge by cookie name, not by string concatenation: a freshly harvested
        # cf_clearance must REPLACE any stale one in the configured cookie, not
        # sit next to it.  Two cf_clearance values in one header get the request
        # rejected (403).  Harvested values win on any name collision.
        cookie = _merge_cookies(cfg.cookie, harvested)

        referer = cfg.referer
        if cookie:
            h["Cookie"] = cookie
            if not referer:
                referer = cfg.default_referer or origin_of(url)
        if referer:
            h["Referer"] = referer

        if cfg.debug:
            log.info(
                "env_cookie=%s harvested=%s referer=%s proxy=%s",
                f"len={len(cfg.cookie)}" if cfg.cookie else "no",
                f"len={len(harvested)}" if harvested else "no",
                "yes" if referer else "no",
                "yes" if cfg.proxy else "no",
            )
        return h

    def _proxies(self) -> dict[str, str] | None:
        p = self.config.proxy
        return {"http": p, "https": p} if p else None

    def _backoff(self, attempt: int) -> None:
        r = self.config.retry
        time.sleep(min(r.backoff_base_sec * (2**attempt), r.backoff_cap_sec))

    # -- fetch -------------------------------------------------------------

    def get(
        self, url: str, *, timeout: int = 60
    ) -> "cffi_requests.Response | BrowserResult":
        """GET *url*, handling Cloudflare via retries, cookie harvest, and — as a
        last resort — a full in-browser fetch.

        Returns a curl_cffi ``Response`` on the normal path, or a
        :class:`~clearway.harvest.BrowserResult` when the browser fetched it;
        both expose ``.text``, ``.content``, and ``.status_code``.  Raises
        :class:`urllib.error.HTTPError` / :class:`~urllib.error.URLError` on
        non-retryable failures and after retries are exhausted.
        """
        url = normalize_request_url(url)
        cfg = self.config
        retry = cfg.retry
        proxies = self._proxies()
        last_exc: HTTPError | URLError | None = None
        harvested_this_call = False

        # Hosts already proven to need the browser: skip the doomed curl attempt.
        if self.fetcher is not None and host_of(url) in self._browser_only:
            result = self._browser_fetch(url)
            if result is not None:
                return result

        attempt = 0
        while attempt <= retry.max_retries:
            try:
                resp = cffi_requests.get(
                    url,
                    headers=self._headers(url),
                    impersonate=cfg.profile.impersonate,
                    timeout=timeout,
                    proxies=proxies,
                )
            except RequestsError as exc:
                last_exc = URLError(str(exc))
                if attempt < retry.max_retries:
                    log.warning(
                        "network error on %s (attempt %d/%d): %r; retrying",
                        url, attempt + 1, retry.max_retries + 1, exc,
                    )
                    self._backoff(attempt)
                    attempt += 1
                    continue
                raise last_exc from exc

            if resp.status_code < 400:
                return resp

            if resp.status_code in retry.retryable_statuses and attempt < retry.max_retries:
                log.warning(
                    "HTTP %d on %s (attempt %d/%d); retrying",
                    resp.status_code, url, attempt + 1, retry.max_retries + 1,
                )
                self._backoff(attempt)
                attempt += 1
                continue

            # Cloudflare JS challenge: solve once per host, then retry without
            # consuming an attempt.  A failed harvest ("") is not cached.
            host = host_of(url)
            if (
                resp.status_code == 403
                and not harvested_this_call
                and host not in self._cf_cookies
            ):
                harvested_this_call = True
                cookie = self.harvester.harvest(
                    origin_of(url), profile=cfg.profile, proxy=cfg.proxy
                )
                if cookie:
                    self._cf_cookies[host] = cookie
                    continue

            # Still blocked (403) — the cookie hand-off to curl_cffi was rejected.
            # Fetch the URL inside the browser itself, where the fingerprint that
            # passed the challenge is the one downloading the content.
            if resp.status_code == 403 and self.fetcher is not None:
                result = self._browser_fetch(url)
                if result is not None:
                    self._browser_only.add(host)
                    return result

            raise HTTPError(
                url,
                resp.status_code,
                f"HTTP Error {resp.status_code}: {resp.reason}",
                resp.headers,  # type: ignore[arg-type]
                BytesIO(resp.content),
            )

        assert last_exc is not None  # loop always returns or raises otherwise
        raise last_exc

    def _browser_fetch(self, url: str) -> "BrowserResult | None":
        """Try to download *url* in the browser; return it only on a <400 status."""
        if self.fetcher is None:
            return None
        result = self.fetcher.fetch(
            url,
            origin=origin_of(url),
            profile=self.config.profile,
            proxy=self.config.proxy,
        )
        if result is not None and result.status_code < 400:
            return result
        return None

    def get_text(self, url: str, *, timeout: int = 60) -> str:
        """Fetch *url* and return its body as text."""
        return self.get(url, timeout=timeout).text

    def get_bytes(self, url: str, *, timeout: int = 180) -> bytes:
        """Fetch *url* and return its body as bytes (PDFs, media, …)."""
        return self.get(url, timeout=timeout).content
