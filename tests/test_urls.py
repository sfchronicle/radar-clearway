from clearway import absolute_url, host_of, normalize_request_url, origin_of


def test_normalize_encodes_spaces_in_path():
    assert normalize_request_url(
        "https://x.gov/a b/c.pdf"
    ) == "https://x.gov/a%20b/c.pdf"


def test_normalize_leaves_query_and_relative_urls():
    assert normalize_request_url("https://x.gov/p?a=b c") == "https://x.gov/p?a=b c"
    assert normalize_request_url("/relative/path") == "/relative/path"


def test_host_of_is_lowercase_and_empty_for_relative():
    assert host_of("https://WWW.NYCourts.GOV/ad3/x") == "www.nycourts.gov"
    assert host_of("/relative") == ""


def test_origin_of():
    assert origin_of("https://x.gov/deep/file.pdf?q=1") == "https://x.gov/"


def test_absolute_url():
    assert absolute_url("https://x.gov/a/b", "../c") == "https://x.gov/c"
