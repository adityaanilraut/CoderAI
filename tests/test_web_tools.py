"""Coverage for the web Tool ``execute()`` coroutines in ``tools/web/tools.py``.

Everything routes through the single ``coderAI.tools.web._safe_request_cf``
transport seam (plus ``_fetch_page_text`` for content enrichment), so each test
patches that one point and feeds fake ``resp`` dicts. This drives ReadURL,
DownloadFile, HTTPRequest, WikipediaSearch, ReadFeed and SitemapDiscover through
their success/error branches — and the sitemap/feed helpers run for real.
"""

import asyncio
from types import SimpleNamespace

from coderAI.core.services import services_scope
from coderAI.tools import web as web_mod
from coderAI.tools.web import tools as tools_mod
from coderAI.tools.web.tools import (
    DownloadFileTool,
    HTTPRequestTool,
    ReadFeedTool,
    ReadURLTool,
    SitemapDiscoverTool,
    WebSearchTool,
    WikipediaSearchTool,
)


def _resp(
    status: int = 200,
    text: str = "",
    content: bytes = b"",
    content_type: str = "text/html",
    url: str = "https://example.com",
    headers=None,
    **extra,
):
    r = {
        "status": status,
        "text": text,
        "content": content,
        "content_type": content_type,
        "url": url,
        "headers": headers or {},
    }
    r.update(extra)
    return r


def _patch_cf(monkeypatch, responder):
    """Patch the transport seam with a ``(method, url, kwargs) -> resp`` responder."""

    async def fake(method, url, **kwargs):
        return responder(method, url, kwargs)

    monkeypatch.setattr(web_mod, "_safe_request_cf", fake)


def _patch_cf_raises(monkeypatch, exc):
    async def fake(method, url, **kwargs):
        raise exc

    monkeypatch.setattr(web_mod, "_safe_request_cf", fake)


VALID_RSS = (
    '<?xml version="1.0"?>'
    '<rss version="2.0"><channel>'
    "<title>My Feed</title><description>a feed</description>"
    "<item><title>Post 1</title><link>https://blog.example.com/1</link>"
    "<description>summary one</description><pubDate>2024</pubDate></item>"
    "<item><title>Post 2</title><link>https://blog.example.com/2</link>"
    "<description>summary two</description></item>"
    "</channel></rss>"
)

VALID_SITEMAP = (
    '<?xml version="1.0"?>'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<url><loc>https://site.example.com/docs/a</loc></url>"
    "<url><loc>https://site.example.com/blog/b</loc></url>"
    "</urlset>"
)


# ── ReadURLTool ─────────────────────────────────────────────────────────


class TestReadURL:
    def test_prepends_scheme_and_returns_content(self, monkeypatch):
        _patch_cf(monkeypatch, lambda m, u, k: _resp(text="<h1>Hi</h1>", url=u))
        result = asyncio.run(ReadURLTool().execute(url="example.com"))
        assert result["success"] is True
        # Scheme was prepended before the request.
        assert result["url"].startswith("https://")

    def test_timeout(self, monkeypatch):
        _patch_cf_raises(monkeypatch, asyncio.TimeoutError())
        result = asyncio.run(ReadURLTool().execute(url="https://example.com"))
        assert result["success"] is False
        assert "Timeout" in result["error"]

    def test_request_exception(self, monkeypatch):
        _patch_cf_raises(monkeypatch, RuntimeError("boom"))
        result = asyncio.run(ReadURLTool().execute(url="https://example.com"))
        assert result["success"] is False
        assert "Failed to fetch" in result["error"]

    def test_ssrf_none(self, monkeypatch):
        _patch_cf(monkeypatch, lambda m, u, k: None)
        result = asyncio.run(ReadURLTool().execute(url="https://example.com"))
        assert result["success"] is False
        assert "SSRF" in result["error"]

    def test_non_200(self, monkeypatch):
        _patch_cf(monkeypatch, lambda m, u, k: _resp(status=404, oversize=True))
        result = asyncio.run(ReadURLTool().execute(url="https://example.com"))
        assert result["success"] is False
        assert "HTTP 404" in result["error"]
        assert result["oversize"] is True

    def test_pdf_extraction_success(self, monkeypatch):
        _patch_cf(
            monkeypatch,
            lambda m, u, k: _resp(content_type="application/pdf", content=b"%PDF-1.4"),
        )
        monkeypatch.setattr(tools_mod, "_extract_pdf_text", lambda c: "Extracted PDF text")
        result = asyncio.run(ReadURLTool().execute(url="https://example.com/doc.pdf"))
        assert result["success"] is True
        assert result["content"] == "Extracted PDF text"
        assert result["content_type"] == "text/plain"

    def test_pdf_extraction_failure(self, monkeypatch):
        _patch_cf(
            monkeypatch,
            lambda m, u, k: _resp(content_type="application/pdf", content=b"%PDF-1.4"),
        )
        monkeypatch.setattr(tools_mod, "_extract_pdf_text", lambda c: None)
        result = asyncio.run(ReadURLTool().execute(url="https://example.com/doc.pdf"))
        assert result["success"] is False
        assert "pypdf" in result["error"]

    def test_extract_main_content(self, monkeypatch):
        html = "<html><body><article><p>main body</p></article></body></html>"
        _patch_cf(monkeypatch, lambda m, u, k: _resp(text=html))
        result = asyncio.run(ReadURLTool().execute(url="https://example.com", extract_main=True))
        assert result["success"] is True
        assert "main body" in result["content"]

    def test_truncation(self, monkeypatch):
        _patch_cf(
            monkeypatch, lambda m, u, k: _resp(text="abcdefghijklmnop", content_type="text/plain")
        )
        result = asyncio.run(ReadURLTool().execute(url="https://example.com", max_length=5))
        assert result["success"] is True
        assert result["truncated"] is True
        assert result["length"] == 5

    def test_extract_metadata(self, monkeypatch):
        html = (
            '<html><head><meta property="og:title" content="OG Title">'
            "<title>Doc</title></head><body>hi</body></html>"
        )
        _patch_cf(monkeypatch, lambda m, u, k: _resp(text=html))
        result = asyncio.run(
            ReadURLTool().execute(url="https://example.com", extract_metadata=True)
        )
        assert result["success"] is True
        assert "metadata" in result
        assert isinstance(result["metadata"], dict)


# ── DownloadFileTool ────────────────────────────────────────────────────


class TestDownloadFile:
    def test_timeout(self, monkeypatch):
        _patch_cf_raises(monkeypatch, asyncio.TimeoutError())
        result = asyncio.run(
            DownloadFileTool().execute(url="https://example.com/f", destination_path="/tmp/x")
        )
        assert result["success"] is False
        assert "Timeout" in result["error"]

    def test_request_exception(self, monkeypatch):
        _patch_cf_raises(monkeypatch, RuntimeError("net down"))
        result = asyncio.run(
            DownloadFileTool().execute(url="https://example.com/f", destination_path="/tmp/x")
        )
        assert result["success"] is False
        assert "Failed to download" in result["error"]

    def test_ssrf_none(self, monkeypatch):
        # Scheme-less URL also exercises the https:// prepend branch.
        _patch_cf(monkeypatch, lambda m, u, k: None)
        result = asyncio.run(
            DownloadFileTool().execute(url="example.com/f", destination_path="/tmp/x")
        )
        assert result["success"] is False
        assert "SSRF" in result["error"]

    def test_protected_path(self, monkeypatch, tmp_path):
        _patch_cf(monkeypatch, lambda m, u, k: _resp(content=b"data"))
        monkeypatch.setattr(tools_mod, "_is_path_protected", lambda p: True)
        result = asyncio.run(
            DownloadFileTool().execute(
                url="https://example.com/f", destination_path=str(tmp_path / "f.bin")
            )
        )
        assert result["success"] is False
        assert "protected path" in result["error"]

    def test_scope_error(self, monkeypatch, tmp_path):
        _patch_cf(monkeypatch, lambda m, u, k: _resp(content=b"data"))
        monkeypatch.setattr(
            tools_mod,
            "_enforce_project_scope",
            lambda dest, name: {"success": False, "error": "out of scope"},
        )
        result = asyncio.run(
            DownloadFileTool().execute(
                url="https://example.com/f", destination_path=str(tmp_path / "f.bin")
            )
        )
        assert result["success"] is False
        assert result["error"] == "out of scope"

    def test_write_failure(self, monkeypatch, tmp_path):
        # destination is an existing directory → open(..., "wb") raises.
        _patch_cf(monkeypatch, lambda m, u, k: _resp(content=b"data"))
        result = asyncio.run(
            DownloadFileTool().execute(url="https://example.com/f", destination_path=str(tmp_path))
        )
        assert result["success"] is False
        assert "Failed to download" in result["error"]


# ── HTTPRequestTool ─────────────────────────────────────────────────────


class TestHTTPRequest:
    def test_invalid_method(self):
        result = asyncio.run(HTTPRequestTool().execute(url="https://example.com", method="TRACE"))
        assert result["success"] is False
        assert "not allowed" in result["error"]

    def test_success_html_to_markdown(self, monkeypatch):
        captured = {}

        async def fake(method, url, **kwargs):
            captured["method"] = method
            captured["headers"] = kwargs.get("headers")
            return _resp(text="<h1>Title</h1>", content_type="text/html", headers={"X": "1"})

        monkeypatch.setattr(web_mod, "_safe_request_cf", fake)
        result = asyncio.run(
            HTTPRequestTool().execute(
                url="example.com", method="post", headers={"Authorization": "Bearer t"}
            )
        )
        assert result["success"] is True
        assert captured["method"] == "POST"
        assert captured["headers"]["Authorization"] == "Bearer t"
        assert "# Title" in result["response"]
        assert result["raw_response"] is not None  # markdown differs from raw html
        assert result["status"] == 200

    def test_ssrf_none(self, monkeypatch):
        _patch_cf(monkeypatch, lambda m, u, k: None)
        result = asyncio.run(HTTPRequestTool().execute(url="https://example.com"))
        assert result["success"] is False
        assert "SSRF" in result["error"]

    def test_timeout(self, monkeypatch):
        _patch_cf_raises(monkeypatch, asyncio.TimeoutError())
        result = asyncio.run(HTTPRequestTool().execute(url="https://example.com", timeout=5))
        assert result["success"] is False
        assert "timed out" in result["error"]

    def test_request_exception(self, monkeypatch):
        _patch_cf_raises(monkeypatch, RuntimeError("refused"))
        result = asyncio.run(HTTPRequestTool().execute(url="https://example.com"))
        assert result["success"] is False
        assert "Request failed" in result["error"]

    def test_truncation_non_html(self, monkeypatch):
        body = "x" * 100
        _patch_cf(monkeypatch, lambda m, u, k: _resp(text=body, content_type="application/json"))
        result = asyncio.run(
            HTTPRequestTool().execute(url="https://example.com", max_response_length=10)
        )
        assert result["success"] is True
        assert result["truncated"] is True
        assert result["response_length"] == 10
        # Non-HTML body is returned verbatim, so raw_response stays None.
        assert result["raw_response"] is None


# ── WikipediaSearchTool ─────────────────────────────────────────────────


class TestWikipediaSearch:
    SEARCH_JSON = (
        '{"query": {"search": ['
        '{"title": "Python", "pageid": 1, "wordcount": 100, "snippet": "<span>prog</span>"}'
        "]}}"
    )

    def test_request_exception(self, monkeypatch):
        _patch_cf_raises(monkeypatch, RuntimeError("dns"))
        result = asyncio.run(WikipediaSearchTool().execute(query="python"))
        assert result["success"] is False
        assert "Wikipedia request failed" in result["error"]

    def test_ssrf_none(self, monkeypatch):
        _patch_cf(monkeypatch, lambda m, u, k: None)
        result = asyncio.run(WikipediaSearchTool().execute(query="python"))
        assert result["success"] is False
        assert "SSRF" in result["error"]

    def test_non_200(self, monkeypatch):
        _patch_cf(monkeypatch, lambda m, u, k: _resp(status=503))
        result = asyncio.run(WikipediaSearchTool().execute(query="python"))
        assert result["success"] is False
        assert "503" in result["error"]

    def test_non_json(self, monkeypatch):
        _patch_cf(monkeypatch, lambda m, u, k: _resp(text="<html>not json</html>"))
        result = asyncio.run(WikipediaSearchTool().execute(query="python"))
        assert result["success"] is False
        assert "non-JSON" in result["error"]

    def test_success(self, monkeypatch):
        _patch_cf(monkeypatch, lambda m, u, k: _resp(text=self.SEARCH_JSON))
        result = asyncio.run(WikipediaSearchTool().execute(query="python", language="EN"))
        assert result["success"] is True
        assert result["language"] == "en"
        assert result["result_count"] == 1
        r0 = result["results"][0]
        assert r0["title"] == "Python"
        assert "Python" in r0["url"]
        assert "prog" in r0["snippet"]  # tags stripped

    def test_fetch_content_swallows_error(self, monkeypatch):
        def responder(method, url, kwargs):
            if "extracts" in url:
                raise RuntimeError("content boom")
            return _resp(text=self.SEARCH_JSON)

        _patch_cf(monkeypatch, responder)
        result = asyncio.run(WikipediaSearchTool().execute(query="python", fetch_content=True))
        # Content-fetch failures are swallowed; search results still returned.
        assert result["success"] is True
        assert "page_content" not in result["results"][0]

    def test_fetch_content_with_truncation(self, monkeypatch):
        content_json = '{"query": {"pages": {"1": {"extract": "%s"}}}}' % ("A" * 200)

        def responder(method, url, kwargs):
            if "extracts" in url:
                return _resp(text=content_json)
            return _resp(text=self.SEARCH_JSON)

        _patch_cf(monkeypatch, responder)
        result = asyncio.run(
            WikipediaSearchTool().execute(query="python", fetch_content=True, max_content_length=50)
        )
        assert result["success"] is True
        r0 = result["results"][0]
        assert "page_content" in r0
        assert "truncated" in r0["page_content"]


# ── ReadFeedTool ────────────────────────────────────────────────────────


class TestReadFeed:
    def test_timeout(self, monkeypatch):
        _patch_cf_raises(monkeypatch, asyncio.TimeoutError())
        result = asyncio.run(ReadFeedTool().execute(url="https://example.com/feed"))
        assert result["success"] is False
        assert "Timeout" in result["error"]

    def test_request_exception(self, monkeypatch):
        _patch_cf_raises(monkeypatch, RuntimeError("nope"))
        result = asyncio.run(ReadFeedTool().execute(url="https://example.com/feed"))
        assert result["success"] is False
        assert "Failed to fetch feed" in result["error"]

    def test_ssrf_none(self, monkeypatch):
        _patch_cf(monkeypatch, lambda m, u, k: None)
        result = asyncio.run(ReadFeedTool().execute(url="https://example.com/feed"))
        assert result["success"] is False
        assert "SSRF" in result["error"]

    def test_non_200(self, monkeypatch):
        _patch_cf(monkeypatch, lambda m, u, k: _resp(status=500))
        result = asyncio.run(ReadFeedTool().execute(url="https://example.com/feed"))
        assert result["success"] is False
        assert "HTTP 500" in result["error"]

    def test_unparseable(self, monkeypatch):
        _patch_cf(monkeypatch, lambda m, u, k: _resp(text="this is not a feed"))
        result = asyncio.run(ReadFeedTool().execute(url="https://example.com/feed"))
        assert result["success"] is False
        assert "Could not parse feed" in result["error"]

    def test_success(self, monkeypatch):
        _patch_cf(monkeypatch, lambda m, u, k: _resp(text=VALID_RSS))
        result = asyncio.run(ReadFeedTool().execute(url="example.com/feed"))
        assert result["success"] is True
        assert result["feed_title"] == "My Feed"
        assert result["entry_count"] == 2
        assert result["entries"][0]["title"] == "Post 1"

    def test_fetch_content(self, monkeypatch):
        _patch_cf(monkeypatch, lambda m, u, k: _resp(text=VALID_RSS))

        async def fake_text(link, max_len, extract_main=False):
            return f"body of {link}"

        monkeypatch.setattr(web_mod, "_fetch_page_text", fake_text)
        result = asyncio.run(
            ReadFeedTool().execute(url="https://example.com/feed", fetch_content=True)
        )
        assert result["success"] is True
        assert result["entries"][0]["page_content"].startswith("body of")

    def test_fetch_content_enrichment_error(self, monkeypatch):
        _patch_cf(monkeypatch, lambda m, u, k: _resp(text=VALID_RSS))

        async def boom(link, max_len, extract_main=False):
            raise RuntimeError("fetch failed")

        monkeypatch.setattr(web_mod, "_fetch_page_text", boom)
        result = asyncio.run(
            ReadFeedTool().execute(url="https://example.com/feed", fetch_content=True)
        )
        # Enrichment failures are swallowed; entries still returned without content.
        assert result["success"] is True
        assert "page_content" not in result["entries"][0]

    def test_fetch_content_entry_without_link(self, monkeypatch):
        feed_no_link = (
            '<?xml version="1.0"?><rss version="2.0"><channel><title>F</title>'
            "<item><title>No Link</title><description>d</description></item>"
            "</channel></rss>"
        )
        _patch_cf(monkeypatch, lambda m, u, k: _resp(text=feed_no_link))

        async def fake_text(link, max_len, extract_main=False):
            raise AssertionError("should not fetch a linkless entry")

        monkeypatch.setattr(web_mod, "_fetch_page_text", fake_text)
        result = asyncio.run(
            ReadFeedTool().execute(url="https://example.com/feed", fetch_content=True)
        )
        assert result["success"] is True
        assert "page_content" not in result["entries"][0]


# ── SitemapDiscoverTool ─────────────────────────────────────────────────


class TestSitemapDiscover:
    def test_robots_discovery(self, monkeypatch):
        def responder(method, url, kwargs):
            if url.endswith("robots.txt"):
                return _resp(text="User-agent: *\nSitemap: https://site.example.com/sitemap.xml")
            return _resp(text=VALID_SITEMAP)

        _patch_cf(monkeypatch, responder)
        result = asyncio.run(SitemapDiscoverTool().execute(url="site.example.com"))
        assert result["success"] is True
        assert result["sitemap_url"] == "https://site.example.com/sitemap.xml"
        assert result["url_count"] == 2

    def test_common_paths_fallback(self, monkeypatch):
        def responder(method, url, kwargs):
            if url.endswith("robots.txt"):
                return _resp(text="User-agent: *\nDisallow: /private")  # no Sitemap line
            if method == "HEAD":
                # First common path probe succeeds.
                return _resp(status=200) if url.endswith("/sitemap.xml") else None
            return _resp(text=VALID_SITEMAP)

        _patch_cf(monkeypatch, responder)
        result = asyncio.run(SitemapDiscoverTool().execute(url="https://site.example.com"))
        assert result["success"] is True
        assert result["sitemap_url"].endswith("/sitemap.xml")

    def test_not_found(self, monkeypatch):
        def responder(method, url, kwargs):
            if url.endswith("robots.txt"):
                return _resp(text="User-agent: *")
            if method == "HEAD":
                return None  # no common path exists
            return _resp(text=VALID_SITEMAP)

        _patch_cf(monkeypatch, responder)
        result = asyncio.run(SitemapDiscoverTool().execute(url="https://site.example.com"))
        assert result["success"] is False
        assert "Could not discover sitemap" in result["error"]

    def test_explicit_sitemap_url(self, monkeypatch):
        _patch_cf(monkeypatch, lambda m, u, k: _resp(text=VALID_SITEMAP))
        result = asyncio.run(
            SitemapDiscoverTool().execute(
                url="https://site.example.com",
                sitemap_url="https://site.example.com/custom.xml",
            )
        )
        assert result["success"] is True
        assert result["sitemap_url"] == "https://site.example.com/custom.xml"
        assert result["total_discovered"] == 2

    def test_filter_path(self, monkeypatch):
        _patch_cf(monkeypatch, lambda m, u, k: _resp(text=VALID_SITEMAP))
        result = asyncio.run(
            SitemapDiscoverTool().execute(
                url="https://site.example.com",
                sitemap_url="https://site.example.com/sitemap.xml",
                filter_path="/docs/",
            )
        )
        assert result["success"] is True
        assert result["urls"] == ["https://site.example.com/docs/a"]


# ── WebSearchTool gaps (cache hit, empty results, fetch_content) ─────────


class TestWebSearchGaps:
    def test_cache_hit(self, monkeypatch):
        cached = [{"title": "T", "url": "https://x.com", "snippet": "s"}]
        monkeypatch.setattr(web_mod, "_get_cached", lambda key: cached)
        result = asyncio.run(WebSearchTool().execute(query="q"))
        assert result["success"] is True
        assert result["from_cache"] is True
        assert result["backend"] == "cache"

    def test_empty_results_note(self, monkeypatch):
        monkeypatch.setattr(web_mod, "_get_cached", lambda key: None)
        monkeypatch.setattr(web_mod, "_select_search_backend", lambda: SimpleNamespace(name="ddg"))

        async def empty_search(self, *a, **k):
            return []

        monkeypatch.setattr(WebSearchTool, "_search", empty_search)
        result = asyncio.run(WebSearchTool().execute(query="q"))
        assert result["success"] is True
        assert result["results"] == []
        assert "No results" in result["note"]

    def test_fetch_content_enriches_top_results(self, monkeypatch):
        monkeypatch.setattr(web_mod, "_select_search_backend", lambda: SimpleNamespace(name="ddg"))
        monkeypatch.setattr(web_mod, "_set_cached", lambda *a, **k: None)

        async def fake_search(self, *a, **k):
            return [{"title": "T", "url": "https://x.com", "snippet": "s"}]

        async def fake_text(url, max_len, extract_main=False):
            return "fetched page body"

        monkeypatch.setattr(WebSearchTool, "_search", fake_search)
        monkeypatch.setattr(web_mod, "_fetch_page_text", fake_text)

        # Config without ``search_cache_ttl_seconds`` exercises the TTL fallback.
        with services_scope(config=SimpleNamespace()):
            result = asyncio.run(WebSearchTool().execute(query="q", fetch_content=True))
        assert result["success"] is True
        assert result["results"][0]["page_content"] == "fetched page body"
        assert result["results"][0]["page_content_length"] == len("fetched page body")

    def test_fetch_content_enrichment_error(self, monkeypatch):
        monkeypatch.setattr(web_mod, "_select_search_backend", lambda: SimpleNamespace(name="ddg"))
        monkeypatch.setattr(web_mod, "_set_cached", lambda *a, **k: None)
        monkeypatch.setattr(web_mod, "_get_cached", lambda key: None)

        async def fake_search(self, *a, **k):
            return [{"title": "T", "url": "https://x.com", "snippet": "s"}]

        async def boom(url, max_len, extract_main=False):
            raise RuntimeError("page fetch failed")

        monkeypatch.setattr(WebSearchTool, "_search", fake_search)
        monkeypatch.setattr(web_mod, "_fetch_page_text", boom)
        result = asyncio.run(WebSearchTool().execute(query="q", fetch_content=True))
        assert result["success"] is True
        assert result["results"][0]["page_content_error"] == "page fetch failed"

    def test_concurrent_search_merges_and_dedups(self, monkeypatch):
        from coderAI.tools.web._search import _SearchResult

        for var in ("CODERAI_SEARCH_BACKEND", "TAVILY_API_KEY", "EXA_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(web_mod, "_get_cached", lambda key: None)
        monkeypatch.setattr(web_mod, "_set_cached", lambda *a, **k: None)
        monkeypatch.setattr(web_mod, "_concurrent_search_enabled", lambda: True)
        monkeypatch.setattr(web_mod, "_select_search_backend", lambda: SimpleNamespace(name="ddg"))

        class FakeBackend:
            name = "fake"

            def __init__(self, results):
                self._results = results

            async def search(self, q, n, allowed=None, blocked=None):
                return self._results

        be1 = FakeBackend([_SearchResult("A", "https://dup.com/x", "s1")])
        be2 = FakeBackend(
            [
                _SearchResult("A2", "https://dup.com/x/", "s2"),  # dup of be1 (trailing /)
                _SearchResult("B", "https://b.com", "s3"),
            ]
        )
        monkeypatch.setattr(web_mod, "_select_free_backends", lambda: [be1, be2])

        cfg = SimpleNamespace(
            tavily_api_key=None,
            exa_api_key=None,
            search_backend="",
            search_cache_ttl_seconds=300,
        )
        with services_scope(config=cfg):
            result = asyncio.run(WebSearchTool().execute(query="q"))
        assert result["success"] is True
        urls = [r["url"] for r in result["results"]]
        assert len(urls) == 2  # the two dup.com URLs collapsed to one
        assert "https://b.com" in urls
