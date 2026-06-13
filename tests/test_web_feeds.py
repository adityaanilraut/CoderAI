"""Coverage for coderAI/tools/web/_feeds.py (RSS/Atom parsing + sitemap discovery)."""

import pytest

from coderAI.tools.web import _feeds


RSS = """junk before xml
<rss version="2.0">
  <channel>
    <title>My Feed</title>
    <description>Feed description</description>
    <item>
      <title>Post 1</title>
      <link>https://example.com/1</link>
      <pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate>
      <description>Summary 1</description>
      <author>Alice</author>
    </item>
    <item>
      <title>Post 2</title>
      <guid>https://example.com/2</guid>
    </item>
  </channel>
</rss>"""

ATOM = """<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Feed</title>
  <entry>
    <title>Entry 1</title>
    <link rel="alternate" href="https://example.com/a1"/>
    <updated>2024-01-01T00:00:00Z</updated>
    <summary>Atom summary</summary>
  </entry>
  <entry>
    <title>Entry 2</title>
    <link rel="self" href="https://example.com/a2"/>
  </entry>
</feed>"""


def test_parse_feed_rss():
    entries = _feeds._parse_feed(RSS, max_entries=10)
    assert len(entries) == 2
    assert entries[0]["title"] == "Post 1"
    assert entries[0]["link"] == "https://example.com/1"
    assert entries[0]["author"] == "Alice"
    assert entries[0]["summary"] == "Summary 1"
    # Second item falls back to <guid> for the link.
    assert entries[1]["link"] == "https://example.com/2"


def test_parse_feed_atom():
    entries = _feeds._parse_feed(ATOM, max_entries=10)
    assert len(entries) == 2
    assert entries[0]["title"] == "Entry 1"
    assert entries[0]["link"] == "https://example.com/a1"
    assert entries[0]["summary"] == "Atom summary"
    # rel="self" only -> resolved by the non-alternate fallback loop.
    assert entries[1]["link"] == "https://example.com/a2"


def test_parse_feed_respects_max_entries():
    assert len(_feeds._parse_feed(RSS, max_entries=1)) == 1


def test_parse_feed_invalid_returns_empty():
    assert _feeds._parse_feed("not xml at all", max_entries=5) == []


def test_extract_feed_metadata_rss():
    meta = _feeds._extract_feed_metadata(RSS)
    assert meta["title"] == "My Feed"
    assert meta["description"] == "Feed description"


def test_extract_feed_metadata_atom():
    meta = _feeds._extract_feed_metadata(ATOM)
    assert meta["title"] == "Atom Feed"


def test_extract_feed_metadata_malformed_returns_empty():
    assert _feeds._extract_feed_metadata("<<<broken") == {}


SITEMAP_URLSET = """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/a</loc></url>
  <url><loc>https://example.com/b</loc></url>
</urlset>"""

SITEMAP_INDEX = """<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/sitemap1.xml</loc></sitemap>
</sitemapindex>"""

SITEMAP_URLSET_NO_NS = """<urlset>
  <url><loc>https://example.com/x</loc></url>
</urlset>"""

SITEMAP_INDEX_NO_NS = """<sitemapindex>
  <sitemap><loc>https://example.com/s.xml</loc></sitemap>
</sitemapindex>"""


def test_parse_sitemap_urlset():
    urls = _feeds._parse_sitemap(SITEMAP_URLSET, max_urls=10)
    assert urls == ["https://example.com/a", "https://example.com/b"]


def test_parse_sitemap_index():
    urls = _feeds._parse_sitemap(SITEMAP_INDEX, max_urls=10)
    assert urls == ["https://example.com/sitemap1.xml"]


def test_parse_sitemap_non_namespaced_fallbacks():
    assert _feeds._parse_sitemap(SITEMAP_URLSET_NO_NS, max_urls=10) == ["https://example.com/x"]
    assert _feeds._parse_sitemap(SITEMAP_INDEX_NO_NS, max_urls=10) == ["https://example.com/s.xml"]


def test_parse_sitemap_respects_max_urls():
    assert len(_feeds._parse_sitemap(SITEMAP_URLSET, max_urls=1)) == 1


def test_parse_sitemap_invalid_returns_empty():
    assert _feeds._parse_sitemap("garbage", max_urls=5) == []


def _patch_cf(monkeypatch, responses):
    """Patch the single _safe_request_cf seam with a queue/callable of responses."""

    async def fake(method, url, timeout_s=10.0):
        if callable(responses):
            return responses(method, url)
        return responses

    monkeypatch.setattr("coderAI.tools.web._safe_request_cf", fake)


async def test_discover_sitemap_from_robots_found(monkeypatch):
    robots = "User-agent: *\nSitemap: https://example.com/sitemap.xml\n"
    _patch_cf(monkeypatch, {"status": 200, "text": robots})
    assert await _feeds._discover_sitemap_from_robots("https://example.com/robots.txt") == (
        "https://example.com/sitemap.xml"
    )


async def test_discover_sitemap_from_robots_absent(monkeypatch):
    _patch_cf(monkeypatch, {"status": 200, "text": "User-agent: *\nDisallow:\n"})
    assert await _feeds._discover_sitemap_from_robots("https://example.com/robots.txt") is None


async def test_discover_sitemap_from_robots_non_200(monkeypatch):
    _patch_cf(monkeypatch, {"status": 404, "text": ""})
    assert await _feeds._discover_sitemap_from_robots("https://example.com/robots.txt") is None


async def test_url_exists_true_false_none(monkeypatch):
    _patch_cf(monkeypatch, {"status": 200, "text": ""})
    assert await _feeds._url_exists("https://example.com/a") is True

    _patch_cf(monkeypatch, {"status": 404, "text": ""})
    assert await _feeds._url_exists("https://example.com/missing") is False

    _patch_cf(monkeypatch, None)
    assert await _feeds._url_exists("https://example.com/none") is False


async def test_fetch_sitemap_urls_success(monkeypatch):
    _patch_cf(monkeypatch, {"status": 200, "text": SITEMAP_URLSET})
    urls = await _feeds._fetch_sitemap_urls("https://example.com/sitemap.xml", max_urls=10)
    assert urls == ["https://example.com/a", "https://example.com/b"]


async def test_fetch_sitemap_urls_non_200(monkeypatch):
    _patch_cf(monkeypatch, {"status": 500, "text": ""})
    assert await _feeds._fetch_sitemap_urls("https://example.com/sitemap.xml", max_urls=10) == []


async def test_fetch_sitemap_urls_request_raises(monkeypatch):
    async def boom(method, url, timeout_s=10.0):
        raise RuntimeError("network down")

    monkeypatch.setattr("coderAI.tools.web._safe_request_cf", boom)
    assert await _feeds._fetch_sitemap_urls("https://example.com/sitemap.xml", max_urls=10) == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
