# radar-clearway

A small, reusable **Cloudflare-aware HTTP layer** for scraping government (and
other protected) sites. Extracted so every scraper repo shares one battle-tested
implementation instead of copy-pasting `curl_cffi` + Playwright glue.

## What it does

- **curl_cffi Chrome TLS impersonation** for the common case — the fingerprint
  and `User-Agent` are pinned *together* in one `ChromeProfile`, so they can
  never drift apart (a version/UA mismatch is a classic bot signal).
- **`cf_clearance` harvest fallback**: on a Cloudflare `403`, it solves the JS
  challenge once in a real browser and **caches the cookie per host**, replaying
  it on later requests — so a batch of PDF downloads pays the browser cost at
  most once per host.
- **Per-host cookie isolation**: `cf_clearance` is host-scoped, so a cookie for
  site A is never replayed against site B (the key fix for multi-site use).
- **Retries with backoff** for network errors and Cloudflare-style 5xx (incl.
  522), raising stdlib `HTTPError` / `URLError` so existing callers keep working.
- **Config injection**: everything site-specific (cookie, referer, proxy,
  profile) lives in `SiteConfig`; the session itself is generic.
- **Swappable browser backend**: the default is Playwright; pass any
  `Harvester` (nodriver, camoufox, FlareSolverr, …) to change it without
  touching the session.

## Install

```bash
pip install git+ssh://git@github.com/sfchronicle/radar-clearway.git
# with the browser fallback:
pip install "radar-clearway[browser] @ git+ssh://git@github.com/sfchronicle/radar-clearway.git"
playwright install chromium --with-deps
```

## Usage

```python
from clearway import CloudflareSession, SiteConfig

# Read cookie/referer/proxy from this scraper's own env namespace.
# Tries prefixes in order; first non-empty wins (new + legacy).
session = CloudflareSession(SiteConfig.from_env("NYCOURTS", "AD3"))

html = session.get_text("https://nycourts.gov/ad3/decisions/index.shtml")
pdf  = session.get_bytes("https://nycourts.gov/.../decision.pdf")
```

`SiteConfig.from_env("NYCOURTS")` reads:

| env var | meaning |
| --- | --- |
| `NYCOURTS_COOKIE` | `Cookie` header from a real browser session |
| `NYCOURTS_REFERER` | `Referer` (defaults to site origin when a cookie is set) |
| `NYCOURTS_PROXY_URL` | proxy for **both** curl_cffi and the browser — use a *sticky* residential proxy, not a rotating one (cf_clearance is IP-bound) |
| `NYCOURTS_DEBUG_HTTP` | `1` logs cookie/proxy presence (lengths only) |

### Swapping the browser backend

```python
from clearway import CloudflareSession, SiteConfig
from clearway.harvest import Harvester

class FlareSolverrHarvester:  # implements Harvester
    def harvest(self, origin, *, profile, proxy=""):
        ...  # return "cf_clearance=...; ..." or "" on failure

session = CloudflareSession(SiteConfig(), harvester=FlareSolverrHarvester())
```

## Notes on bypassing Cloudflare

- The strongest lever is usually the **IP**, not the fingerprint. Datacenter IPs
  (GitHub Actions) are flagged regardless of impersonation; a sticky residential
  proxy (`*_PROXY_URL`) is the highest-ROI fix.
- **Avoid the challenge when you can.** Many sites gate only their HTML pages
  while serving the real asset (PDF, video) from an unprotected CDN. Fetching the
  direct asset URL is more reliable than any bypass.

## Layout

```
src/clearway/
  profile.py   ChromeProfile (impersonate + UA paired) + CHROME146
  config.py    SiteConfig (+ from_env), RetryPolicy
  urls.py      normalize_request_url, absolute_url, host_of, origin_of
  harvest.py   Harvester protocol + PlaywrightHarvester
  session.py   CloudflareSession (per-host cache, retries, 403 harvest)
```
