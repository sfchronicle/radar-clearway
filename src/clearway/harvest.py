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
from pathlib import Path
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


class BrowserResult:
    """A resource fetched inside the browser — a page's HTML or a file's bytes."""

    def __init__(self, status_code: int, content: bytes, text: str):
        self.status_code = status_code
        self.content = content
        self.text = text


class BrowserFetcher(Protocol):
    """Downloads a URL *inside* a real browser and returns its body.

    Unlike a :class:`Harvester` (which hands a cookie to curl_cffi and can be
    rejected on a fingerprint mismatch), this fetches the content with the very
    browser that passed the challenge — no hand-off — so a strict site that
    rejects the cookie replay still works.  Slower than curl, needs no proxy.
    Returns ``None`` if the fetch could not be completed.  :meth:`close` releases
    the browser.
    """

    def fetch(
        self, url: str, *, origin: str, profile: ChromeProfile, proxy: str = ""
    ) -> "BrowserResult | None": ...

    def close(self) -> None: ...


class PlaywrightFetcher:
    """Fetch URLs inside one long-lived headless Chromium.

    The browser is opened lazily on first use and reused for every later fetch,
    so a batch of downloads pays the launch cost once and solves each host's
    challenge once (the cf_clearance stays in the browser's own cookie jar).
    ``playwright`` is imported lazily, so the package works without the
    ``[browser]`` extra until a fetch is actually attempted.
    """

    def __init__(self, solve_timeout_sec: float = 25.0, nav_timeout_ms: int = 60_000):
        self.solve_timeout_sec = solve_timeout_sec
        self.nav_timeout_ms = nav_timeout_ms
        self._pw = None
        self._browser = None
        self._context = None
        self._cleared_hosts: set[str] = set()

    def _ensure_context(self, profile: ChromeProfile, proxy: str) -> bool:
        """Open the browser + context once; return False if playwright is missing."""
        if self._context is not None:
            return True
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.warning(
                "playwright not installed — cannot browser-fetch. "
                "Install the extra: pip install 'radar-clearway[browser]' && "
                "playwright install chromium --with-deps"
            )
            return False
        self._pw = sync_playwright().start()
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
        self._browser = self._pw.chromium.launch(**launch_kwargs)
        self._context = self._browser.new_context(
            user_agent=profile.user_agent,
            locale="en-US",
            viewport={"width": 1366, "height": 768},
            accept_downloads=True,  # some sites serve PDFs as attachments
        )
        self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        return True

    def fetch(
        self, url: str, *, origin: str, profile: ChromeProfile, proxy: str = ""
    ) -> "BrowserResult | None":
        if not self._ensure_context(profile, proxy):
            return None
        assert self._context is not None
        try:
            page = self._context.new_page()
            downloads: list = []
            page.on("download", lambda d: downloads.append(d))
            try:
                host = urlparse(url).hostname or ""
                # Solve the challenge once per host, on the origin root; the
                # cf_clearance then lives in the shared context for later fetches.
                if host not in self._cleared_hosts:
                    page.goto(origin, wait_until="load", timeout=self.nav_timeout_ms)
                    deadline = time.time() + self.solve_timeout_sec
                    while time.time() < deadline:
                        if any(c["name"] == "cf_clearance" for c in self._context.cookies()):
                            break
                        page.wait_for_timeout(500)
                    self._cleared_hosts.add(host)

                # Navigating to a file the server marks as an attachment makes
                # Chromium download it instead of rendering — goto then raises
                # "Download is starting" / ERR_ABORTED, and the bytes arrive via
                # a download event instead.  Handle both.
                resp = None
                try:
                    resp = page.goto(url, wait_until="load", timeout=self.nav_timeout_ms)
                except Exception as nav_exc:  # noqa: BLE001
                    msg = str(nav_exc)
                    if "Download is starting" not in msg and "ERR_ABORTED" not in msg:
                        raise

                if resp is not None:
                    content = resp.body()
                    try:
                        text = resp.text()
                    except Exception:  # noqa: BLE001
                        text = content.decode("utf-8", "replace")
                    if resp.status >= 400:
                        # Log the block-page title so we can tell a solvable
                        # challenge ("Just a moment…") from a hard IP/WAF block
                        # ("Sorry, you have been blocked" / "Attention Required").
                        try:
                            page_title = page.title()
                        except Exception:  # noqa: BLE001
                            page_title = ""
                        log.info(
                            "browser-fetch blocked: %s -> HTTP %d, title=%r (%d bytes)",
                            url, resp.status, page_title[:120], len(content),
                        )
                    else:
                        log.info(
                            "browser-fetched %s (HTTP %d, %d bytes)",
                            url, resp.status, len(content),
                        )
                    return BrowserResult(resp.status, content, text)

                # Download path: wait for the file, then read its bytes.
                deadline = time.time() + 20
                while not downloads and time.time() < deadline:
                    page.wait_for_timeout(200)
                if downloads:
                    data = Path(downloads[0].path()).read_bytes()
                    log.info("browser-downloaded %s (%d bytes)", url, len(data))
                    return BrowserResult(200, data, data.decode("utf-8", "replace"))
                return None
            finally:
                page.close()
        except Exception as exc:  # noqa: BLE001 — best-effort fallback, never fatal
            log.warning("browser fetch failed for %s: %r", url, exc)
            return None

    def close(self) -> None:
        for obj in (self._context, self._browser):
            try:
                if obj is not None:
                    obj.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._pw = self._browser = self._context = None
        self._cleared_hosts.clear()
