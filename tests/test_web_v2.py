"""Tests for the new web tool features.

Covers: search backend selection (Tavily / Exa / DDG), domain filtering,
read_url format selector, oversize cap, and Cloudflare UA fallback.
"""

import asyncio
import json

import pytest

from coderAI.tools import web as web_mod
from coderAI.tools.web import (
    ReadURLTool,
    WebSearchTool,
    _convert_content,
    _DDGBackend,
    _ExaBackend,
    _filter_by_domain,
    _is_cloudflare_block,
    _safe_request_cf,
    _SearchResult,
    _select_search_backend,
    _TavilyBackend,
    _TRANSPARENT_UA,
)


# ---------------------------------------------------------------------------
# HTML → format conversion
# ---------------------------------------------------------------------------


class TestConvertContent:
    def test_markdown_format_renders_headings(self):
        out = _convert_content("<h1>Title</h1><p>body</p>", "text/html", "markdown")
        assert "# Title" in out
        assert "body" in out

    def test_markdown_format_renders_links(self):
        out = _convert_content(
            '<p>Visit <a href="https://example.com">example</a></p>',
            "text/html",
            "markdown",
        )
        assert "[example](https://example.com)" in out

    def test_text_format_strips_all_markup(self):
        out = _convert_content("<h1>Title</h1><ul><li>a</li><li>b</li></ul>", "text/html", "text")
        assert "<" not in out
        assert "#" not in out
        assert "Title" in out
        assert "a" in out and "b" in out

    def test_html_format_returns_raw(self):
        raw = "<h1>Title</h1><p>body</p>"
        assert _convert_content(raw, "text/html", "html") == raw

    def test_non_html_passthrough(self):
        raw = "plain text"
        assert _convert_content(raw, "text/plain", "markdown") == raw
        assert _convert_content(raw, "text/plain", "text") == raw


# ---------------------------------------------------------------------------
# Search backend selection
# ---------------------------------------------------------------------------


class TestBackendSelection:
    def test_default_is_ddg(self, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        monkeypatch.delenv("EXA_API_KEY", raising=False)
        monkeypatch.delenv("CODERAI_SEARCH_BACKEND", raising=False)
        backend = _select_search_backend()
        assert backend.name == "ddg"

    def test_auto_prefers_tavily_over_exa(self, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tk")
        monkeypatch.setenv("EXA_API_KEY", "ek")
        monkeypatch.delenv("CODERAI_SEARCH_BACKEND", raising=False)
        backend = _select_search_backend()
        assert backend.name == "tavily"

    def test_auto_picks_exa_when_only_exa_set(self, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        monkeypatch.setenv("EXA_API_KEY", "ek")
        monkeypatch.delenv("CODERAI_SEARCH_BACKEND", raising=False)
        backend = _select_search_backend()
        assert backend.name == "exa"

    def test_explicit_ddg_override(self, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tk")
        monkeypatch.setenv("CODERAI_SEARCH_BACKEND", "ddg")
        backend = _select_search_backend()
        assert backend.name == "ddg"

    def test_explicit_tavily_without_key_falls_back(self, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        monkeypatch.delenv("EXA_API_KEY", raising=False)
        monkeypatch.setenv("CODERAI_SEARCH_BACKEND", "tavily")
        backend = _select_search_backend()
        assert backend.name == "ddg"


# ---------------------------------------------------------------------------
# Tavily backend
# ---------------------------------------------------------------------------


class TestTavilyBackend:
    def test_parses_results_and_passes_filters(self, monkeypatch):
        captured = {}

        async def fake(method, url, **kw):
            captured["url"] = url
            captured["body"] = kw.get("json_body")
            return {
                "status": 200,
                "headers": {},
                "url": url,
                "content_type": "application/json",
                "text": json.dumps(
                    {
                        "results": [
                            {"title": "T1", "url": "https://a.com/1", "content": "snip1"},
                            {"title": "T2", "url": "https://b.com/2", "content": "snip2"},
                        ]
                    }
                ),
                "content": b"",
            }

        monkeypatch.setattr(web_mod, "_safe_request", fake)
        backend = _TavilyBackend("test-key")
        results = asyncio.run(
            backend.search(
                "hello",
                num_results=5,
                allowed_domains=["a.com"],
                blocked_domains=["b.com"],
            )
        )

        assert captured["url"] == _TavilyBackend.ENDPOINT
        assert captured["body"]["api_key"] == "test-key"
        assert captured["body"]["query"] == "hello"
        assert captured["body"]["include_domains"] == ["a.com"]
        assert captured["body"]["exclude_domains"] == ["b.com"]
        assert len(results) == 2
        assert results[0].title == "T1"
        assert results[0].snippet == "snip1"

    def test_raises_on_http_error(self, monkeypatch):
        async def fake(method, url, **kw):
            return {
                "status": 500,
                "headers": {},
                "url": url,
                "content_type": "text/plain",
                "text": "server error",
                "content": b"",
            }

        monkeypatch.setattr(web_mod, "_safe_request", fake)
        backend = _TavilyBackend("k")
        with pytest.raises(RuntimeError, match="Tavily HTTP 500"):
            asyncio.run(backend.search("q", 5))

    def test_raises_on_ssrf_block(self, monkeypatch):
        async def fake(method, url, **kw):
            return None

        monkeypatch.setattr(web_mod, "_safe_request", fake)
        backend = _TavilyBackend("k")
        with pytest.raises(RuntimeError, match="SSRF guard"):
            asyncio.run(backend.search("q", 5))


# ---------------------------------------------------------------------------
# Exa backend
# ---------------------------------------------------------------------------


class TestExaBackend:
    def test_uses_api_key_header(self, monkeypatch):
        captured = {}

        async def fake(method, url, **kw):
            captured["headers"] = kw.get("headers")
            captured["body"] = kw.get("json_body")
            return {
                "status": 200,
                "headers": {},
                "url": url,
                "content_type": "application/json",
                "text": json.dumps(
                    {"results": [{"title": "T", "url": "https://x.com", "text": "snippet"}]}
                ),
                "content": b"",
            }

        monkeypatch.setattr(web_mod, "_safe_request", fake)
        backend = _ExaBackend("exa-key")
        results = asyncio.run(backend.search("q", 3))

        assert captured["headers"]["x-api-key"] == "exa-key"
        assert captured["body"]["query"] == "q"
        assert captured["body"]["numResults"] == 3
        assert results[0].title == "T"
        assert results[0].snippet == "snippet"


# ---------------------------------------------------------------------------
# Domain filtering
# ---------------------------------------------------------------------------


class TestDomainFilter:
    def _r(self, url):
        return _SearchResult(title="t", url=url, snippet="s")

    def test_allowed_filters_out_other_domains(self):
        results = [self._r("https://a.com/x"), self._r("https://b.com/y")]
        out = _filter_by_domain(results, allowed=["a.com"], blocked=None)
        assert len(out) == 1
        assert out[0].url == "https://a.com/x"

    def test_allowed_suffix_match(self):
        results = [self._r("https://docs.example.com/x"), self._r("https://other.com/y")]
        out = _filter_by_domain(results, allowed=["example.com"], blocked=None)
        assert len(out) == 1
        assert "example.com" in out[0].url

    def test_blocked_drops_matching(self):
        results = [self._r("https://a.com/x"), self._r("https://b.com/y")]
        out = _filter_by_domain(results, allowed=None, blocked=["b.com"])
        assert len(out) == 1
        assert out[0].url == "https://a.com/x"

    def test_blocked_takes_priority_over_allowed(self):
        results = [self._r("https://a.com/x")]
        out = _filter_by_domain(results, allowed=["a.com"], blocked=["a.com"])
        assert out == []


# ---------------------------------------------------------------------------
# Cloudflare UA fallback
# ---------------------------------------------------------------------------


class TestCloudflareFallback:
    def test_detects_cf_mitigated_header(self):
        assert _is_cloudflare_block(403, {"cf-mitigated": "challenge"}) is True

    def test_detects_cloudflare_server_header(self):
        assert _is_cloudflare_block(403, {"Server": "cloudflare"}) is True

    def test_ignores_normal_403(self):
        assert _is_cloudflare_block(403, {"Server": "nginx"}) is False

    def test_ignores_200(self):
        assert _is_cloudflare_block(200, {"cf-ray": "abc"}) is False

    def test_retries_with_transparent_ua_on_cf_block(self, monkeypatch):
        calls = []

        async def fake(method, url, **kw):
            hdrs = kw.get("headers") or {}
            calls.append(hdrs.get("User-Agent"))
            if len(calls) == 1:
                # First call: CF block
                return {
                    "status": 403,
                    "headers": {"cf-mitigated": "challenge"},
                    "url": url,
                    "content_type": "text/html",
                    "text": "blocked",
                    "content": b"",
                }
            # Second call: success
            return {
                "status": 200,
                "headers": {},
                "url": url,
                "content_type": "text/html",
                "text": "ok",
                "content": b"",
            }

        monkeypatch.setattr(web_mod, "_safe_request", fake)
        result = asyncio.run(_safe_request_cf("GET", "https://example.com"))
        assert len(calls) == 2
        assert calls[1] == _TRANSPARENT_UA
        assert result["status"] == 200

    def test_no_retry_when_not_cf_block(self, monkeypatch):
        calls = []

        async def fake(method, url, **kw):
            calls.append(1)
            return {
                "status": 403,
                "headers": {"Server": "nginx"},
                "url": url,
                "content_type": "text/html",
                "text": "forbidden",
                "content": b"",
            }

        monkeypatch.setattr(web_mod, "_safe_request", fake)
        result = asyncio.run(_safe_request_cf("GET", "https://example.com"))
        assert len(calls) == 1
        assert result["status"] == 403


# ---------------------------------------------------------------------------
# Oversize cap
# ---------------------------------------------------------------------------


class TestOversize:
    def test_oversize_flag_propagates(self, monkeypatch):
        async def fake(method, url, **kw):
            return {
                "status": 200,
                "headers": {},
                "url": url,
                "content_type": "text/html",
                "text": "<html>ok</html>",
                "content": b"",
                "oversize": True,
            }

        # _safe_request_cf passes through when the response is 200 (no CF check
        # for non-block statuses), so monkeypatching _safe_request is enough.
        monkeypatch.setattr(web_mod, "_safe_request", fake)
        tool = ReadURLTool()
        result = asyncio.run(tool.execute(url="https://example.com"))
        assert result["success"] is True
        assert result["oversize"] is True


# ---------------------------------------------------------------------------
# read_url format param
# ---------------------------------------------------------------------------


class TestReadURLFormat:
    def _stub(self, monkeypatch, body: str):
        async def fake(method, url, **kw):
            return {
                "status": 200,
                "headers": {"Content-Type": "text/html"},
                "url": url,
                "content_type": "text/html",
                "text": body,
                "content": body.encode("utf-8"),
            }

        monkeypatch.setattr(web_mod, "_safe_request", fake)

    def test_html_format_returns_raw(self, monkeypatch):
        self._stub(monkeypatch, "<h1>Hi</h1>")
        result = asyncio.run(
            ReadURLTool().execute(url="https://example.com", format="html")
        )
        assert result["success"] is True
        assert result["format"] == "html"
        assert "<h1>" in result["content"]

    def test_text_format_strips_markup(self, monkeypatch):
        self._stub(monkeypatch, "<h1>Hi</h1><p>body</p>")
        result = asyncio.run(
            ReadURLTool().execute(url="https://example.com", format="text")
        )
        assert result["success"] is True
        assert result["format"] == "text"
        assert "<" not in result["content"]
        assert "#" not in result["content"]
        assert "Hi" in result["content"]

    def test_markdown_format_default(self, monkeypatch):
        self._stub(monkeypatch, "<h1>Hi</h1>")
        result = asyncio.run(ReadURLTool().execute(url="https://example.com"))
        assert result["success"] is True
        assert result["format"] == "markdown"
        assert "# Hi" in result["content"]

    def test_invalid_format_rejected(self, monkeypatch):
        result = asyncio.run(
            ReadURLTool().execute(url="https://example.com", format="bogus")
        )
        assert result["success"] is False
        assert "Invalid format" in result["error"]


# ---------------------------------------------------------------------------
# WebSearch with allowed/blocked domains
# ---------------------------------------------------------------------------


class TestWebSearchDomainFilter:
    def test_allowed_domains_passed_to_filter(self, monkeypatch):
        async def fake_backend_search(self, query, n, allowed=None, blocked=None):
            return [
                _SearchResult("T1", "https://allowed.com/x", "s"),
                _SearchResult("T2", "https://blocked.com/y", "s"),
            ]

        monkeypatch.setattr(_DDGBackend, "search", fake_backend_search)
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        monkeypatch.delenv("EXA_API_KEY", raising=False)
        monkeypatch.delenv("CODERAI_SEARCH_BACKEND", raising=False)

        result = asyncio.run(
            WebSearchTool().execute(
                query="q",
                allowed_domains=["allowed.com"],
            )
        )
        assert result["success"] is True
        assert len(result["results"]) == 1
        assert "allowed.com" in result["results"][0]["url"]

    def test_backend_name_in_response(self, monkeypatch):
        async def fake_backend_search(self, query, n, allowed=None, blocked=None):
            return [_SearchResult("T", "https://x.com", "s")]

        monkeypatch.setattr(_DDGBackend, "search", fake_backend_search)
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        monkeypatch.delenv("EXA_API_KEY", raising=False)
        monkeypatch.delenv("CODERAI_SEARCH_BACKEND", raising=False)

        result = asyncio.run(WebSearchTool().execute(query="q"))
        assert result["backend"] == "ddg"
