"""Per-site configuration for a :class:`~clearway.session.CloudflareSession`.

Everything a session needs that is *site-specific* lives here — cookie, referer,
proxy, fingerprint profile, retry policy — so the session itself stays generic.
Build one directly, or via :meth:`SiteConfig.from_env` to read a scraper's own
env-var namespace (e.g. ``NYCOURTS_COOKIE`` / ``NYCOURTS_PROXY_URL``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace

from .profile import DEFAULT_PROFILE, ChromeProfile


def _normalize_cookie_header(raw: str) -> str:
    """Strip whitespace and an accidental leading ``Cookie:`` from pasted values."""
    s = raw.strip()
    for prefix in ("cookie:", "Cookie:"):
        if s.startswith(prefix):
            return s[len(prefix) :].lstrip()
    return s


@dataclass
class RetryPolicy:
    """How transient failures are retried (capped exponential backoff)."""

    max_retries: int = 3
    backoff_base_sec: float = 1.0
    backoff_cap_sec: float = 8.0
    # Transient HTTP statuses worth retrying: the standard 5xx-ish set plus the
    # Cloudflare-specific 52x family (522 "origin connection time-out" is common).
    retryable_statuses: frozenset[int] = frozenset(
        {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 527}
    )


@dataclass
class SiteConfig:
    """Configuration for requests to one site.

    Attributes:
        profile:  fingerprint profile (impersonate + matching UA), paired so they
                  cannot drift apart.  Defaults to :data:`~clearway.profile.CHROME146`.
        cookie:   a ``Cookie`` header value from a real browser session, if any.
        referer:  ``Referer`` header to send.  When empty and a cookie is
                  present, falls back to ``default_referer`` if set, else the
                  site origin at request time.
        default_referer: the ``Referer`` used when ``referer`` is empty but a
                  cookie is present.  Empty means "use the site origin".  Set
                  this to pin a specific landing page (e.g. a site's index).
        proxy:    proxy URL applied to BOTH curl_cffi and the browser harvester,
                  e.g. ``http://user:pass@host:port``.  Use a *sticky/session*
                  residential proxy, not a rotating one — cf_clearance is IP-bound.
        debug:    if True, the session logs cookie/proxy presence (lengths only).
        retry:    retry/backoff policy for transient failures.
    """

    profile: ChromeProfile = DEFAULT_PROFILE
    cookie: str = ""
    referer: str = ""
    default_referer: str = ""
    proxy: str = ""
    debug: bool = False
    retry: RetryPolicy = field(default_factory=RetryPolicy)

    def __post_init__(self) -> None:
        self.cookie = _normalize_cookie_header(self.cookie) if self.cookie else ""

    @classmethod
    def from_env(
        cls,
        *prefixes: str,
        profile: ChromeProfile = DEFAULT_PROFILE,
        env: dict[str, str] | None = None,
    ) -> "SiteConfig":
        """Build a config from ``<PREFIX>_COOKIE`` / ``_REFERER`` / ``_PROXY_URL`` / ``_DEBUG_HTTP``.

        Multiple prefixes are tried in order and the first non-empty value for
        each field wins, which lets a scraper honor both a new and a legacy
        namespace (e.g. ``SiteConfig.from_env("AD", "AD3")``).  Prefixes are
        joined to field names with ``_``; pass them without the trailing
        underscore.
        """
        source = os.environ if env is None else env

        def first(suffix: str) -> str:
            for prefix in prefixes:
                v = source.get(f"{prefix}_{suffix}", "").strip()
                if v:
                    return v
            return ""

        return cls(
            profile=profile,
            cookie=first("COOKIE"),
            referer=first("REFERER"),
            proxy=first("PROXY_URL"),
            debug=first("DEBUG_HTTP") == "1",
        )

    def with_(self, **changes: object) -> "SiteConfig":
        """Return a copy with the given fields replaced (config is otherwise shared)."""
        return replace(self, **changes)
