"""A minimal end-to-end example: fetch a page with clearway, parse it with BeautifulSoup.

Run it:
    pip install radar-clearway beautifulsoup4
    python examples/simple_scraper.py

clearway does the FETCHING (Chrome TLS impersonation, plus a browser-based
cf_clearance fallback if Cloudflare challenges it). BeautifulSoup does the
PARSING (pulling data out of the HTML). You never call `requests` yourself —
`session.get_text(url)` returns the page as a string directly.
"""

from bs4 import BeautifulSoup

from clearway import CloudflareSession, SiteConfig


def main() -> None:
    # One session per site. from_env("DEMO") reads optional DEMO_COOKIE /
    # DEMO_REFERER / DEMO_PROXY_URL / DEMO_DEBUG_HTTP from the environment.
    # None are required: with nothing set, it just fetches with a Chrome
    # fingerprint and solves a Cloudflare challenge automatically if one shows up.
    session = CloudflareSession(SiteConfig.from_env("DEMO"))

    # 1. FETCH — this one call replaces `requests.get(url).text`.
    #    `html` is now the full page as a string; fetching is done.
    url = "https://example.com/"
    html = session.get_text(url)
    print(f"Fetched {len(html)} characters from {url}\n")

    # 2. PARSE — turn that string into something you can search.
    soup = BeautifulSoup(html, "html.parser")

    # 3. EXTRACT — pull out whatever you need. Here: the title and every link.
    title = soup.title.string if soup.title else "(no title)"
    print(f"Page title: {title}\n")

    print("Links found:")
    for a in soup.find_all("a", href=True):
        print(f"  - {a.get_text(strip=True)} -> {a['href']}")

    # To download a file (PDF, image, ...) instead of a page, use get_bytes:
    #   data = session.get_bytes("https://example.com/report.pdf")
    #   Path("report.pdf").write_bytes(data)


if __name__ == "__main__":
    main()
