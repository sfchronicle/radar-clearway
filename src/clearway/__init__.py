"""clearway — a Cloudflare-aware HTTP layer for scraping (esp. government sites).

curl_cffi Chrome TLS impersonation for the common case; a browser-based
``cf_clearance`` harvest (cached per host) as the 403 fallback; retries with
backoff; all site-specific settings injected via :class:`SiteConfig`.

Quick start::

    from clearway import CloudflareSession, SiteConfig

    session = CloudflareSession(SiteConfig.from_env("NYCOURTS"))
    html = session.get_text("https://nycourts.gov/ad3/...")
    pdf = session.get_bytes("https://nycourts.gov/.../decision.pdf")

Swap the browser backend (nodriver, FlareSolverr, …) by passing a custom
``harvester`` implementing :class:`~clearway.harvest.Harvester`.
"""

from __future__ import annotations

from .config import RetryPolicy, SiteConfig
from .harvest import (
    BrowserFetcher,
    BrowserResult,
    Harvester,
    PlaywrightFetcher,
    PlaywrightHarvester,
)
from .profile import CHROME146, DEFAULT_PROFILE, ChromeProfile
from .session import CloudflareSession
from .urls import absolute_url, host_of, normalize_request_url, origin_of

__version__ = "0.1.0"

__all__ = [
    "CloudflareSession",
    "SiteConfig",
    "RetryPolicy",
    "ChromeProfile",
    "CHROME146",
    "DEFAULT_PROFILE",
    "Harvester",
    "PlaywrightHarvester",
    "BrowserFetcher",
    "PlaywrightFetcher",
    "BrowserResult",
    "normalize_request_url",
    "absolute_url",
    "host_of",
    "origin_of",
]
