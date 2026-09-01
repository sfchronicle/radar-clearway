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

from .config import SiteConfig
from .harvest import Harvester, PlaywrightHarvester
from .urls import host_of, normalize_request_url, origin_of

log = logging.getLogger("clearway.session")


class CloudflareSession:
    """Fetches URLs for one site, transparently handling Cloudflare challenges.

    Args:
        config:    per-site settings.  Defaults to a bare :class:`SiteConfig`
                   (default Chrome profile, no cookie/proxy).
        harvester: browser backend used to solve challenges.  Defaults to
                   :class:`~clearway.harvest.PlaywrightHarvester`; pass your own
                   (nodriver, FlareSolverr, …) to swap the browser without
                   touching this class.
    """

    def __init__(
        self,
        config: SiteConfig | None = None,
        *,
        harvester: Harvester | None = None,
    ):
        self.config = config or SiteConfig()
        self.harvester: Harvester = harvester or PlaywrightHarvester()
        # host -> harvested cookie string (cf_clearance etc.).  cf_clearance is
        # host-scoped, so it is never shared across hosts.
        self._cf_cookies: dict[str, str] = {}

    # -- headers -----------------------------------------------------------

    def _headers(self, url: str) -> dict[str, str]:
        """User-Agent / Cookie / Referer for a request to *url*.

        Only User-Agent is forced (to match the harvester's UA); curl_cffi's
        impersonate keeps supplying the matching sec-ch-ua client hints.
        """
        cfg = self.config
        h: dict[str, str] = {"User-Agent": cfg.profile.user_agent}

        harvested = self._cf_cookies.get(host_of(url), "")
        cookie = "; ".join(filter(None, [cfg.cookie, harvested]))

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

    def get(self, url: str, *, timeout: int = 60) -> cffi_requests.Response:
        """GET *url* with impersonation, retries, and Cloudflare 403 harvesting.

        Raises :class:`urllib.error.HTTPError` / :class:`~urllib.error.URLError`
        on non-retryable failures and after retries are exhausted.
        """
        url = normalize_request_url(url)
        cfg = self.config
        retry = cfg.retry
        proxies = self._proxies()
        last_exc: HTTPError | URLError | None = None
        harvested_this_call = False

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

            raise HTTPError(
                url,
                resp.status_code,
                f"HTTP Error {resp.status_code}: {resp.reason}",
                resp.headers,  # type: ignore[arg-type]
                BytesIO(resp.content),
            )

        assert last_exc is not None  # loop always returns or raises otherwise
        raise last_exc

    def get_text(self, url: str, *, timeout: int = 60) -> str:
        """Fetch *url* and return its body as text."""
        return self.get(url, timeout=timeout).text

    def get_bytes(self, url: str, *, timeout: int = 180) -> bytes:
        """Fetch *url* and return its body as bytes (PDFs, media, …)."""
        return self.get(url, timeout=timeout).content
