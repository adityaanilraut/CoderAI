"""Coverage for the web Tool ``execute()`` coroutines in ``tools/web/tools.py``.

Everything routes through the single ``coderAI.tools.web._safe_request_cf``
transport seam (plus ``_fetch_page_text`` for content enrichment), so each test
patches that one point and feeds fake ``resp`` dicts. This drives ReadURL,
DownloadFile and HTTPRequest through
their success/error branches — and the sitemap/feed helpers run for real.
"""

import asyncio
from types import SimpleNamespace

from coderAI.core.services import services_scope
from coderAI.types.tool_error_codes import ToolErrorCode
from coderAI.tools import web as web_mod
from coderAI.tools.filesystem import ProjectPathError
from coderAI.tools.web import tools as tools_mod
from coderAI.tools.web.tools import (
    DownloadFileTool,
    HTTPRequestTool,
    ReadURLTool,
    WebSearchTool,
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
        monkeypatch.setattr(web_mod, "_get_cached", lambda key: None)
        monkeypatch.setattr(web_mod, "_set_cached", lambda *a, **k: None)
        _patch_cf(monkeypatch, lambda m, u, k: _resp(text="<h1>Hi</h1>", url=u))
        result = asyncio.run(ReadURLTool().execute(url="example.com"))
        assert result["success"] is True
        # Scheme was prepended before the request.
        assert result["url"].startswith("https://")

    def test_timeout(self, monkeypatch):
        monkeypatch.setattr(web_mod, "_get_cached", lambda key: None)
        _patch_cf_raises(monkeypatch, asyncio.TimeoutError())
        result = asyncio.run(ReadURLTool().execute(url="https://example.com"))
        assert result["success"] is False
        assert "Timeout" in result["error"]

    def test_request_exception(self, monkeypatch):
        monkeypatch.setattr(web_mod, "_get_cached", lambda key: None)
        _patch_cf_raises(monkeypatch, RuntimeError("boom"))
        result = asyncio.run(ReadURLTool().execute(url="https://example.com"))
        assert result["success"] is False
        assert "Failed to fetch" in result["error"]

    def test_ssrf_none(self, monkeypatch):
        monkeypatch.setattr(web_mod, "_get_cached", lambda key: None)
        _patch_cf(monkeypatch, lambda m, u, k: None)
        result = asyncio.run(ReadURLTool().execute(url="https://example.com"))
        assert result["success"] is False
        assert "SSRF" in result["error"]

    def test_non_200(self, monkeypatch):
        monkeypatch.setattr(web_mod, "_get_cached", lambda key: None)
        _patch_cf(monkeypatch, lambda m, u, k: _resp(status=404, oversize=True))
        result = asyncio.run(ReadURLTool().execute(url="https://example.com"))
        assert result["success"] is False
        assert "HTTP 404" in result["error"]
        assert result["oversize"] is True

    def test_pdf_extraction_success(self, monkeypatch):
        monkeypatch.setattr(web_mod, "_get_cached", lambda key: None)
        monkeypatch.setattr(web_mod, "_set_cached", lambda *a, **k: None)
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
        monkeypatch.setattr(web_mod, "_get_cached", lambda key: None)
        _patch_cf(
            monkeypatch,
            lambda m, u, k: _resp(content_type="application/pdf", content=b"%PDF-1.4"),
        )
        monkeypatch.setattr(tools_mod, "_extract_pdf_text", lambda c: None)
        result = asyncio.run(ReadURLTool().execute(url="https://example.com/doc.pdf"))
        assert result["success"] is False
        assert "pypdf" in result["error"]

    def test_extract_main_content(self, monkeypatch):
        monkeypatch.setattr(web_mod, "_get_cached", lambda key: None)
        monkeypatch.setattr(web_mod, "_set_cached", lambda *a, **k: None)
        html = "<html><body><article><p>main body</p></article></body></html>"
        _patch_cf(monkeypatch, lambda m, u, k: _resp(text=html))
        result = asyncio.run(ReadURLTool().execute(url="https://example.com", extract_main=True))
        assert result["success"] is True
        assert "main body" in result["content"]

    def test_truncation(self, monkeypatch):
        monkeypatch.setattr(web_mod, "_get_cached", lambda key: None)
        monkeypatch.setattr(web_mod, "_set_cached", lambda *a, **k: None)
        _patch_cf(
            monkeypatch, lambda m, u, k: _resp(text="abcdefghijklmnop", content_type="text/plain")
        )
        result = asyncio.run(ReadURLTool().execute(url="https://example.com", max_length=5))
        assert result["success"] is True
        assert result["truncated"] is True
        assert result["length"] == 5

    def test_extract_metadata(self, monkeypatch):
        monkeypatch.setattr(web_mod, "_get_cached", lambda key: None)
        monkeypatch.setattr(web_mod, "_set_cached", lambda *a, **k: None)
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
        calls = []
        _patch_cf(monkeypatch, lambda m, u, k: calls.append(1))
        monkeypatch.setattr(
            tools_mod,
            "resolve_under_project",
            lambda *a, **k: (_ for _ in ()).throw(
                ProjectPathError("protected path", ToolErrorCode.PERMISSION_DENIED)
            ),
        )
        result = asyncio.run(
            DownloadFileTool().execute(
                url="https://example.com/f", destination_path=str(tmp_path / "f.bin")
            )
        )
        assert result["success"] is False
        assert "protected path" in result["error"]
        assert calls == []

    def test_scope_error(self, monkeypatch, tmp_path):
        calls = []
        _patch_cf(monkeypatch, lambda m, u, k: calls.append(1))
        monkeypatch.setattr(
            tools_mod,
            "resolve_under_project",
            lambda *a, **k: (_ for _ in ()).throw(
                ProjectPathError("out of scope", ToolErrorCode.SCOPE)
            ),
        )
        result = asyncio.run(
            DownloadFileTool().execute(
                url="https://example.com/f", destination_path=str(tmp_path / "f.bin")
            )
        )
        assert result["success"] is False
        assert result["error"] == "out of scope"
        assert calls == []

    def test_oversize_preserves_existing_destination(self, monkeypatch, tmp_path):
        destination = tmp_path / "existing.bin"
        destination.write_bytes(b"original")
        _patch_cf(
            monkeypatch,
            lambda m, u, k: _resp(content=b"partial", oversize=True),
        )

        result = asyncio.run(
            DownloadFileTool().execute(
                url="https://example.com/large.bin",
                destination_path=str(destination),
            )
        )

        assert result["success"] is False
        assert result["error_code"] == "too_large"
        assert destination.read_bytes() == b"original"

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
            return [], "ddg"

        monkeypatch.setattr(WebSearchTool, "_search", empty_search)
        result = asyncio.run(WebSearchTool().execute(query="q"))
        assert result["success"] is True
        assert result["results"] == []
        assert "No results" in result["note"]

    def test_fetch_content_enriches_top_results(self, monkeypatch):
        monkeypatch.setattr(web_mod, "_select_search_backend", lambda: SimpleNamespace(name="ddg"))
        monkeypatch.setattr(web_mod, "_set_cached", lambda *a, **k: None)

        async def fake_search(self, *a, **k):
            return [{"title": "T", "url": "https://x.com", "snippet": "s"}], "ddg"

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
            return [{"title": "T", "url": "https://x.com", "snippet": "s"}], "ddg"

        async def boom(url, max_len, extract_main=False):
            raise RuntimeError("page fetch failed")

        monkeypatch.setattr(WebSearchTool, "_search", fake_search)
        monkeypatch.setattr(web_mod, "_fetch_page_text", boom)
        result = asyncio.run(WebSearchTool().execute(query="q", fetch_content=True))
        assert result["success"] is True
        assert result["results"][0]["page_content_error"] == "page fetch failed"

    def test_fetch_content_does_not_pollute_snippet_cache(self, monkeypatch):
        """Content-enriched results must not be written under the snippet cache key."""
        written = []
        monkeypatch.setattr(web_mod, "_get_cached", lambda key: None)
        monkeypatch.setattr(web_mod, "_set_cached", lambda key, val, ttl: written.append(val))
        monkeypatch.setattr(web_mod, "_select_search_backend", lambda: SimpleNamespace(name="ddg"))

        async def fake_search(self, *a, **k):
            return [{"title": "T", "url": "https://x.com", "snippet": "s"}], "ddg"

        async def fake_text(url, max_len, extract_main=False):
            return "page body"

        monkeypatch.setattr(WebSearchTool, "_search", fake_search)
        monkeypatch.setattr(web_mod, "_fetch_page_text", fake_text)
        with services_scope(config=SimpleNamespace(search_cache_ttl_seconds=300)):
            asyncio.run(WebSearchTool().execute(query="q", fetch_content=True))
        assert written == []

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
        assert result["backend"] == "ddg+searxng"
        urls = [r["url"] for r in result["results"]]
        assert len(urls) == 2  # the two dup.com URLs collapsed to one
        assert "https://b.com" in urls

    def test_read_url_uses_page_cache(self, monkeypatch):
        calls = {"n": 0}

        def fake_cf(method, url, kwargs):
            calls["n"] += 1
            return _resp(text="<p>hello cache</p>", url=url)

        _patch_cf(monkeypatch, fake_cf)
        monkeypatch.setattr(web_mod, "_get_cached", lambda key: None)
        stored = {}

        def set_cached(key, val, ttl):
            stored["key"] = key
            stored["val"] = val

        monkeypatch.setattr(web_mod, "_set_cached", set_cached)
        with services_scope(config=SimpleNamespace(page_cache_ttl_seconds=3600)):
            first = asyncio.run(ReadURLTool().execute(url="https://example.com"))
        assert first["success"] is True
        assert calls["n"] == 1
        assert "content" in stored["val"]

        monkeypatch.setattr(web_mod, "_get_cached", lambda key: stored["val"])
        second = asyncio.run(ReadURLTool().execute(url="https://example.com"))
        assert second["success"] is True
        assert second.get("from_cache") is True
        assert calls["n"] == 1  # no second network fetch
        assert "hello" in second["content"].lower() or "cache" in second["content"].lower()
