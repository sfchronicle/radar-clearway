"""Cloudflare ``cf_clearance`` harvesting via a real browser.

When curl_cffi gets a 403 (Cloudflare's JS challenge), the only way to obtain a
valid ``cf_clearance`` is to solve the challenge in a real browser running on the
*same IP and User-Agent* the later requests will use — cf_clearance is bound to
both.  A :class:`Harvester` does exactly that and returns the resulting cookies.

The interface is deliberately small so the browser backend is swappable: the
default :class:`PlaywrightHarvester` can be replaced with a nodriver/camoufox/
FlareSolverr-backed one without touching the session.
"""

from __future__ import annotations

import logging
import time
from typing import Protocol
from urllib.parse import urlparse

from .profile import ChromeProfile

log = logging.getLogger("clearway.harvest")


class Harvester(Protocol):
    """Solves a Cloudflare challenge and returns a ``Cookie`` header value.

    Returns the harvested cookie string (containing ``cf_clearance``) on success,
    or ``""`` if the challenge could not be solved — callers must treat ``""`` as
    "give up / raise the original 403", and must NOT cache an empty result so a
    later request is free to try again.
    """

    def harvest(self, origin: str, *, profile: ChromeProfile, proxy: str = "") -> str: ...


def _playwright_proxy(proxy: str) -> dict[str, str]:
    """Convert ``scheme://user:pass@host:port`` into Playwright's proxy dict."""
    p = urlparse(proxy)
    server = f"{p.scheme}://{p.hostname}"
    if p.port:
        server += f":{p.port}"
    setting: dict[str, str] = {"server": server}
    if p.username:
        setting["username"] = p.username
    if p.password:
        setting["password"] = p.password
    return setting


class PlaywrightHarvester:
    """Solve the challenge with a headless Chromium via ``playwright``.

    Import of playwright is deferred to :meth:`harvest` so the package installs
    and imports fine without the ``[browser]`` extra; a harvest simply fails
    (returns ``""``) if playwright is not present.
    """

    def __init__(self, solve_timeout_sec: float = 25.0, nav_timeout_ms: int = 60_000):
        self.solve_timeout_sec = solve_timeout_sec
        self.nav_timeout_ms = nav_timeout_ms

    def harvest(self, origin: str, *, profile: ChromeProfile, proxy: str = "") -> str:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.warning(
                "playwright not installed — cannot solve Cloudflare challenge. "
                "Install the extra: pip install 'radar-clearway[browser]' && "
                "playwright install chromium --with-deps"
            )
            return ""

        log.info(
            "solving Cloudflare challenge on %s (proxy=%s)",
            origin,
            "on" if proxy else "off",
        )
        try:
            with sync_playwright() as pw:
                launch_kwargs: dict = {
                    "headless": True,
                    "args": [
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                    ],
                }
                if proxy:
                    launch_kwargs["proxy"] = _playwright_proxy(proxy)

                browser = pw.chromium.launch(**launch_kwargs)
                context = browser.new_context(
                    user_agent=profile.user_agent,  # MUST match curl_cffi's UA
                    locale="en-US",
                    viewport={"width": 1366, "height": 768},
                )
                context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                )
                page = context.new_page()

                # "load", not "networkidle": the challenge page keeps polling, so
                # networkidle never fires and we would time out.
                page.goto(origin, wait_until="load", timeout=self.nav_timeout_ms)

                deadline = time.time() + self.solve_timeout_sec
                while time.time() < deadline:
                    if any(c["name"] == "cf_clearance" for c in context.cookies()):
                        break
                    page.wait_for_timeout(500)

                cookies = context.cookies()
                browser.close()

            if not any(c["name"] == "cf_clearance" for c in cookies):
                log.warning(
                    "browser ran but no cf_clearance issued (%d other cookies) — "
                    "challenge not solved. If this persists the block is likely "
                    "IP-based: use a sticky residential proxy.",
                    len(cookies),
                )
                return ""

            harvested = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            log.info("solved — %d cookies (cf_clearance=yes)", len(cookies))
            return harvested
        except Exception as exc:  # noqa: BLE001 — best-effort fallback, never fatal
            log.warning("browser harvest failed: %r", exc)
            return ""
