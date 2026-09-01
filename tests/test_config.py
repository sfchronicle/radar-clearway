from clearway import CHROME146, SiteConfig


def test_from_env_reads_prefixed_vars():
    env = {
        "NYCOURTS_COOKIE": "a=1; b=2",
        "NYCOURTS_PROXY_URL": "http://u:p@host:8080",
        "NYCOURTS_DEBUG_HTTP": "1",
    }
    cfg = SiteConfig.from_env("NYCOURTS", env=env)
    assert cfg.cookie == "a=1; b=2"
    assert cfg.proxy == "http://u:p@host:8080"
    assert cfg.debug is True
    assert cfg.referer == ""


def test_from_env_first_prefix_wins():
    env = {"AD3_COOKIE": "legacy=1", "AD_COOKIE": "new=1"}
    cfg = SiteConfig.from_env("AD", "AD3", env=env)
    assert cfg.cookie == "new=1"  # AD_ tried before AD3_


def test_from_env_falls_back_to_legacy_prefix():
    env = {"AD3_COOKIE": "legacy=1"}
    cfg = SiteConfig.from_env("AD", "AD3", env=env)
    assert cfg.cookie == "legacy=1"


def test_cookie_header_prefix_is_stripped():
    cfg = SiteConfig(cookie="Cookie: a=1; b=2")
    assert cfg.cookie == "a=1; b=2"


def test_default_profile_is_chrome146_and_paired():
    cfg = SiteConfig()
    assert cfg.profile is CHROME146
    assert cfg.profile.impersonate == "chrome146"
    assert "Chrome/146" in cfg.profile.user_agent
    # UA platform must match the impersonate profile's platform (macOS).
    assert "Macintosh" in cfg.profile.user_agent
