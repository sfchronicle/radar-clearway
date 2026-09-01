# radar-clearway

A small, reusable **Cloudflare-aware HTTP layer** for scraping government (and
other protected) sites. Extracted so every scraper repo shares one battle-tested
implementation instead of copy-pasting `curl_cffi` + Playwright glue.

> 📊 **New to this? Start with the plain-language visual explainer (中文):**
> [How clearway gets past Cloudflare](docs/how-it-works.html) — the whole
> knock → checkpoints → get-the-stamp → reuse-it flow as a picture, no code.

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

## Adopting clearway in a new scraper repo

A three-step checklist to wire clearway into another `radar-*` (or any) repo.

### 1. Add the dependency (pinned to a tag)

In `requirements.txt` (or `pyproject.toml`):

```
radar-clearway[browser] @ git+https://github.com/sfchronicle/radar-clearway.git@v0.1.0
```

- Drop `[browser]` if the repo never needs the Playwright `cf_clearance`
  fallback (i.e. its targets are not behind a hard Cloudflare challenge) — it
  then installs only `curl_cffi` and stays lightweight.
- **Pin a tag**, not `@main`, so a change on clearway's `main` can't silently
  alter your production scraping. Bump the tag deliberately to adopt a release.

### 2. Use a session

Pick your repo's own env-var prefix (e.g. `CTGOV`) so its settings never collide
with another repo's:

```python
from clearway import CloudflareSession, SiteConfig

session = CloudflareSession(SiteConfig.from_env("CTGOV"))
html = session.get_text("https://portal.ct.gov/...")
pdf  = session.get_bytes("https://portal.ct.gov/.../doc.pdf")
```

That reads `CTGOV_COOKIE` / `CTGOV_REFERER` / `CTGOV_PROXY_URL` /
`CTGOV_DEBUG_HTTP` — **all optional**. With none set it just fetches with the
Chrome fingerprint and harvests `cf_clearance` automatically on a 403. No cookie
is required (see the table above).

Zero-config and proxy-only forms both work too:

```python
CloudflareSession()                                     # nothing configured
CloudflareSession(SiteConfig(proxy="http://u:p@host:port"))  # datacenter IPs
```

### 3. GitHub Actions

Because `radar-clearway` is public, **no auth is needed** — the existing
`pip install -r requirements.txt` step clones it anonymously. If the repo uses
the browser fallback, keep the browser-binary step after install:

```yaml
- run: pip install -r requirements.txt
- run: playwright install chromium --with-deps   # only if you use [browser]
```

Pass this scraper's secrets in as its prefixed env vars on the step that fetches:

```yaml
    env:
      CTGOV_COOKIE: ${{ secrets.CTGOV_COOKIE }}
      CTGOV_PROXY_URL: ${{ secrets.CTGOV_PROXY_URL }}
```

### Migrating an existing HTTP module

If a repo already has its own `requests`/`curl_cffi` helper, make it a thin shim
so nothing downstream changes (this is exactly what
`radar-albany-appellate-decisions`'s `ad_http.py` does):

```python
# old_http.py — now a shim over clearway
from clearway import CloudflareSession, SiteConfig, absolute_url, normalize_request_url

_session = CloudflareSession(SiteConfig.from_env("AD", "AD3"))

def fetch_text(url, timeout=60):  return _session.get_text(url, timeout=timeout)
def fetch_bytes(url, timeout=180): return _session.get_bytes(url, timeout=timeout)
```

`from_env` accepts multiple prefixes tried in order (first non-empty wins), so a
new namespace can coexist with a legacy one during migration.

### Getting past a persistent 403

If a site keeps returning 403 even with clearway, the ladder, in order of ROI:

1. **Set `<PREFIX>_PROXY_URL` to a sticky residential proxy.** Datacenter IPs
   (GitHub Actions) are flagged regardless of fingerprint; this is usually the
   fix. Use a session/sticky proxy, not a rotating one — `cf_clearance` is
   IP-bound.
2. **Look for a direct asset/CDN URL** (PDF, video, JSON API) that isn't behind
   the challenge, and fetch that instead.
3. **Swap in a stronger `Harvester`** (nodriver / FlareSolverr) via the
   constructor — only if 1 and 2 don't cover it.

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
