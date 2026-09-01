"""Session behavior tests with curl_cffi fully mocked (no network)."""

from __future__ import annotations

import pytest

import clearway.session as session_mod
from clearway import CloudflareSession, SiteConfig


class FakeResp:
    def __init__(self, status=200, text="ok", content=b"ok", reason="OK"):
        self.status_code = status
        self.text = text
        self.content = content
        self.reason = reason
        self.headers = {}


class FakeHarvester:
    """Records which origins it was asked to solve and returns a per-host cookie."""

    def __init__(self):
        self.calls: list[str] = []

    def harvest(self, origin, *, profile, proxy=""):
        self.calls.append(origin)
        host = origin.split("//", 1)[1].rstrip("/")
        return f"cf_clearance={host}-token"


def install_fake_get(monkeypatch, script):
    """Patch cffi_requests.get with a scripted responder.

    ``script`` maps url -> list of FakeResp returned on successive calls.
    Also records the headers each call was made with, in ``sent``.
    """
    sent: list[dict] = []

    def fake_get(url, headers=None, impersonate=None, timeout=None, proxies=None):
        sent.append({"url": url, "headers": headers or {}})
        queue = script[url]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(session_mod.cffi_requests, "get", fake_get)
    return sent


def test_200_passes_through(monkeypatch):
    install_fake_get(monkeypatch, {"https://a.gov/x": [FakeResp(text="hello")]})
    s = CloudflareSession(harvester=FakeHarvester())
    assert s.get_text("https://a.gov/x") == "hello"


def test_403_triggers_harvest_then_retries_with_cookie(monkeypatch):
    url = "https://a.gov/x"
    sent = install_fake_get(
        monkeypatch, {url: [FakeResp(status=403, reason="Forbidden"), FakeResp(text="unlocked")]}
    )
    harv = FakeHarvester()
    s = CloudflareSession(harvester=harv)

    assert s.get_text(url) == "unlocked"
    # Harvested exactly once, on the origin root.
    assert harv.calls == ["https://a.gov/"]
    # Second (successful) request carried the harvested cf_clearance cookie.
    assert "cf_clearance=a.gov-token" in sent[1]["headers"]["Cookie"]


def test_cookie_is_cached_per_host_no_reharvest(monkeypatch):
    install_fake_get(
        monkeypatch,
        {
            "https://a.gov/1": [FakeResp(status=403), FakeResp(text="one")],
            "https://a.gov/2": [FakeResp(text="two")],
        },
    )
    harv = FakeHarvester()
    s = CloudflareSession(harvester=harv)

    assert s.get_text("https://a.gov/1") == "one"
    assert s.get_text("https://a.gov/2") == "two"
    # Only the first request harvested; the second reused the cached cookie.
    assert harv.calls == ["https://a.gov/"]


def test_cookies_isolated_between_hosts(monkeypatch):
    sent = install_fake_get(
        monkeypatch,
        {
            "https://a.gov/x": [FakeResp(status=403), FakeResp(text="a")],
            "https://b.gov/y": [FakeResp(status=403), FakeResp(text="b")],
        },
    )
    harv = FakeHarvester()
    s = CloudflareSession(harvester=harv)

    s.get_text("https://a.gov/x")
    s.get_text("https://b.gov/y")

    # Each host solved independently.
    assert harv.calls == ["https://a.gov/", "https://b.gov/"]
    # b.gov's request must NOT carry a.gov's cookie (the multi-site fix).
    b_retry = [r for r in sent if r["url"] == "https://b.gov/y"][-1]
    assert "b.gov-token" in b_retry["headers"]["Cookie"]
    assert "a.gov-token" not in b_retry["headers"]["Cookie"]


def test_failed_harvest_not_cached_and_raises_403(monkeypatch):
    url = "https://a.gov/x"
    install_fake_get(monkeypatch, {url: [FakeResp(status=403, reason="Forbidden")]})

    class DeadHarvester:
        calls = 0

        def harvest(self, origin, *, profile, proxy=""):
            DeadHarvester.calls += 1
            return ""  # could not solve

    s = CloudflareSession(harvester=DeadHarvester())
    from urllib.error import HTTPError

    with pytest.raises(HTTPError):
        s.get_text(url)
    # Nothing cached, so a later call is free to try harvesting again.
    assert url not in [h for h in s._cf_cookies]
    assert DeadHarvester.calls == 1


def test_referer_defaults_when_cookie_present(monkeypatch):
    url = "https://a.gov/deep/x"
    # default_referer set -> used verbatim
    sent = install_fake_get(monkeypatch, {url: [FakeResp(text="ok")]})
    s = CloudflareSession(SiteConfig(cookie="k=1", default_referer="https://a.gov/index"))
    s.get_text(url)
    assert sent[0]["headers"]["Referer"] == "https://a.gov/index"

    # no default_referer -> site origin
    sent = install_fake_get(monkeypatch, {url: [FakeResp(text="ok")]})
    s = CloudflareSession(SiteConfig(cookie="k=1"))
    s.get_text(url)
    assert sent[0]["headers"]["Referer"] == "https://a.gov/"


def test_no_referer_without_cookie(monkeypatch):
    url = "https://a.gov/x"
    sent = install_fake_get(monkeypatch, {url: [FakeResp(text="ok")]})
    s = CloudflareSession(SiteConfig(default_referer="https://a.gov/index"))
    s.get_text(url)
    # default_referer only kicks in when a cookie is present.
    assert "Referer" not in sent[0]["headers"]


def test_retryable_status_is_retried(monkeypatch):
    url = "https://a.gov/x"
    install_fake_get(monkeypatch, {url: [FakeResp(status=522), FakeResp(text="recovered")]})
    cfg = SiteConfig()
    cfg.retry.backoff_base_sec = 0  # no real sleep in tests
    s = CloudflareSession(cfg, harvester=FakeHarvester())
    assert s.get_text(url) == "recovered"
