"""URL helpers shared by the HTTP layer."""

from __future__ import annotations

from urllib.parse import quote, urljoin, urlparse, urlunparse


def normalize_request_url(url: str) -> str:
    """Percent-encode the path (e.g. spaces in filenames) so servers accept it.

    Leaves the scheme, host, query, and fragment untouched.  Non-absolute URLs
    are returned unchanged.
    """
    p = urlparse(url)
    if not p.scheme or not p.netloc:
        return url
    path = quote(p.path, safe="/")
    return urlunparse((p.scheme, p.netloc, path, p.params, p.query, p.fragment))


def absolute_url(base: str, href: str) -> str:
    """Resolve *href* against *base* (thin wrapper over :func:`urllib.parse.urljoin`)."""
    return urljoin(base, href)


def host_of(url: str) -> str:
    """Return the lowercase hostname of *url*, or '' if it has none.

    Used as the cache key for per-host Cloudflare cookies: ``cf_clearance`` is
    scoped to a single host, so cookies harvested for one host must never be
    replayed against another.
    """
    return (urlparse(url).hostname or "").lower()


def origin_of(url: str) -> str:
    """Return ``scheme://host/`` for *url* — the site root a challenge is solved on.

    ``cf_clearance`` covers the whole host, and Cloudflare's interstitial does not
    reliably render for non-HTML targets (e.g. a ``.pdf``), so the challenge is
    solved on the origin root and the cookie reused for the real request.
    """
    p = urlparse(url)
    if p.scheme and p.netloc:
        return f"{p.scheme}://{p.netloc}/"
    return url
