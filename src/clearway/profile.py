"""Browser fingerprint profiles.

A :class:`ChromeProfile` pins the curl_cffi ``impersonate`` target and the
``User-Agent`` header *together*, in one object, so they can never drift apart.

Why this matters: ``impersonate`` drives both the TLS/JA3 fingerprint AND the
``sec-ch-ua`` client-hint headers curl_cffi sends.  If code sets ``impersonate``
to one Chrome build but overrides ``User-Agent`` with a different one, the JA3 /
client hints advertise version A while the UA header claims version B — a
mismatch Cloudflare reads as a bot signal.  Bundling the two here means a caller
picks a profile, not two independently-editable strings.

The default :data:`CHROME146` UA is the exact string curl_cffi's ``chrome146``
profile emits natively (verified via an httpbin echo), so User-Agent, sec-ch-ua,
and sec-ch-ua-platform all agree.  To add a profile for another Chrome build,
read its native UA once and copy it verbatim::

    python -c "from curl_cffi import requests; \\
        print(requests.get('https://httpbin.org/headers', \\
        impersonate='chrome146').json()['headers']['User-Agent'])"
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChromeProfile:
    """A paired curl_cffi impersonate target and its matching User-Agent.

    ``impersonate`` must be a target your installed ``curl_cffi`` supports (e.g.
    ``"chrome146"``), and ``user_agent`` must be the exact UA that target emits
    natively — see the module docstring for how to derive it.
    """

    impersonate: str
    user_agent: str


# curl_cffi chrome146's native macOS User-Agent (verified 2026 against 0.15.0).
# Keeping the version + platform identical across UA / sec-ch-ua / sec-ch-ua-platform.
CHROME146 = ChromeProfile(
    impersonate="chrome146",
    user_agent=(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36"
    ),
)

# The default profile used when a SiteConfig does not name one.
DEFAULT_PROFILE = CHROME146
