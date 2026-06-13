"""Coverage for coderAI/tools/web/_search.py parsers, selection, and backends."""

import json
from types import SimpleNamespace

import pytest

from coderAI.core.services import services_scope
from coderAI.tools.web import _search as s


# ── URL resolution ─────────────────────────────────────────────────────


def test_resolve_ddg_url_redirect_and_passthrough():
    redirect = "//duckduckgo.com/l/?uddg=https%3A%2F%2Freal.example.com%2Fp&rut=abc"
    assert s._resolve_ddg_url(redirect) == "https://real.example.com/p"

    # y.js variant resolves the same way.
    yjs = "https://duckduckgo.com/y.js?uddg=https%3A%2F%2Fads.example.com"
    assert s._resolve_ddg_url(yjs) == "https://ads.example.com"

    # Protocol-relative non-redirect gets https prepended.
    assert s._resolve_ddg_url("//cdn.example.com/x") == "https://cdn.example.com/x"
    # Plain URL passes through (with &amp; decoded).
    assert s._resolve_ddg_url("https://e.com/a?x=1&amp;y=2") == "https://e.com/a?x=1&y=2"


# ── DDG result parsers ──────────────────────────────────────────────────

DDG_HTML = (
    '<a href="https://example.com/page" class="result-link">Example Title</a>'
    '<td class="result-snippet">An example snippet</td>'
    '<a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fsecond.com" class="result-link">Second</a>'
    '<td class="result-snippet">second snippet</td>'
)


def test_parse_ddg_results_regex():
    results = s._parse_ddg_results(DDG_HTML, max_results=10)
    assert len(results) == 2
    assert results[0].url == "https://example.com/page"
    assert results[0].title == "Example Title"
    assert "example snippet" in results[0].snippet
    # Redirect URL resolved.
    assert results[1].url == "https://second.com"


def test_parse_ddg_results_respects_max():
    assert len(s._parse_ddg_results(DDG_HTML, max_results=1)) == 1


def test_parse_ddg_results_v2_htmlparser():
    results = s._parse_ddg_results_v2(DDG_HTML, max_results=10)
    assert results
    assert any(r.url == "https://example.com/page" for r in results)


def test_parse_ddg_results_v2_row_url_fallback():
    # No result-link/snippet structure, but bare http anchors exist.
    html = '<a href="https://fallback.example.com/x">link</a>'
    results = s._parse_ddg_results_v2(html, max_results=5)
    assert results
    assert results[0].url == "https://fallback.example.com/x"


# ── SearXNG parser ──────────────────────────────────────────────────────


def test_parse_searxng_results_with_content_class():
    html = (
        '<article class="result">'
        '<a href="https://sx.example.com/a">SX Title</a>'
        '<p class="content">A searxng snippet</p>'
        "</article>"
    )
    results = s._parse_searxng_results(html, max_results=10)
    assert len(results) == 1
    assert results[0].url == "https://sx.example.com/a"
    assert results[0].title == "SX Title"
    assert "searxng snippet" in results[0].snippet


def test_parse_searxng_results_any_p_fallback():
    # No snippet class -> longest <p> wins.
    html = (
        '<article class="result">'
        '<a href="https://sx.example.com/b">B</a>'
        "<p>short</p><p>a noticeably longer paragraph of text</p>"
        "</article>"
    )
    results = s._parse_searxng_results(html, max_results=10)
    assert results[0].snippet == "a noticeably longer paragraph of text"


def test_parse_searxng_skips_non_http_and_blockless():
    html = (
        '<article class="result"><a href="ftp://nope">x</a></article>'
        '<article class="result">no link here</article>'
    )
    assert s._parse_searxng_results(html, max_results=10) == []


# ── Domain filtering ────────────────────────────────────────────────────


def test_domain_helpers():
    assert s._domain_of("https://Sub.Example.com/path") == "sub.example.com"
    assert s._domain_of("not a url") == ""
    assert s._matches_domain("docs.python.org", ["python.org"]) is True
    assert s._matches_domain("evil.com", ["python.org"]) is False
    assert s._matches_domain("x.com", [".", ""]) is False


def test_filter_by_domain_allow_and_block():
    results = [
        s._SearchResult("a", "https://python.org/a", ""),
        s._SearchResult("b", "https://evil.com/b", ""),
        s._SearchResult("c", "https://docs.python.org/c", ""),
    ]
    allowed = s._filter_by_domain(results, allowed=["python.org"], blocked=None)
    assert {r.url for r in allowed} == {"https://python.org/a", "https://docs.python.org/c"}

    blocked = s._filter_by_domain(results, allowed=None, blocked=["evil.com"])
    assert all("evil.com" not in r.url for r in blocked)


# ── Backend selection ───────────────────────────────────────────────────


def _cfg(**kw):
    base = dict(
        search_backend="",
        tavily_api_key=None,
        exa_api_key=None,
        concurrent_search=True,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _clear_env(monkeypatch):
    for var in ("CODERAI_SEARCH_BACKEND", "TAVILY_API_KEY", "EXA_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_select_backend_auto_defaults_to_ddg(monkeypatch):
    _clear_env(monkeypatch)
    with services_scope(config=_cfg()):
        assert isinstance(s._select_search_backend(), s._DDGBackend)


def test_select_backend_auto_prefers_tavily_then_exa(monkeypatch):
    _clear_env(monkeypatch)
    with services_scope(config=_cfg(tavily_api_key="tk")):
        assert isinstance(s._select_search_backend(), s._TavilyBackend)
    with services_scope(config=_cfg(exa_api_key="ek")):
        assert isinstance(s._select_search_backend(), s._ExaBackend)


def test_select_backend_explicit_env(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CODERAI_SEARCH_BACKEND", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "tk")
    with services_scope(config=_cfg()):
        assert isinstance(s._select_search_backend(), s._TavilyBackend)

    # Explicit tavily without a key falls back to DDG.
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    with services_scope(config=_cfg()):
        assert isinstance(s._select_search_backend(), s._DDGBackend)


def test_select_backend_explicit_exa_and_ddg(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CODERAI_SEARCH_BACKEND", "exa")
    with services_scope(config=_cfg(exa_api_key="ek")):
        assert isinstance(s._select_search_backend(), s._ExaBackend)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    with services_scope(config=_cfg()):
        assert isinstance(s._select_search_backend(), s._DDGBackend)  # exa, no key -> ddg

    monkeypatch.setenv("CODERAI_SEARCH_BACKEND", "ddg")
    with services_scope(config=_cfg()):
        assert isinstance(s._select_search_backend(), s._DDGBackend)


def test_select_backend_config_value_used(monkeypatch):
    _clear_env(monkeypatch)
    with services_scope(config=_cfg(search_backend="tavily", tavily_api_key="tk")):
        assert isinstance(s._select_search_backend(), s._TavilyBackend)


def test_concurrent_search_enabled_reads_config():
    with services_scope(config=_cfg(concurrent_search=True)):
        assert s._concurrent_search_enabled() is True
    with services_scope(config=_cfg(concurrent_search=False)):
        assert s._concurrent_search_enabled() is False


def test_select_free_backends():
    backends = s._select_free_backends()
    assert isinstance(backends[0], s._DDGBackend)
    assert isinstance(backends[1], s._SearXNGBackend)


# ── Backend .search() methods (patched transport) ───────────────────────


def _patch_request(monkeypatch, responder):
    async def fake(method, url, **kwargs):
        return responder(method, url, kwargs)

    monkeypatch.setattr("coderAI.tools.web._safe_request", fake)


async def test_tavily_backend_search(monkeypatch):
    payload = {"results": [{"title": "T", "url": "https://t.com", "content": "snip"}]}
    _patch_request(monkeypatch, lambda *a: {"status": 200, "text": json.dumps(payload)})
    results = await s._TavilyBackend("k").search("q", 5, ["t.com"], ["bad.com"])
    assert results[0].url == "https://t.com"
    assert results[0].snippet == "snip"


async def test_tavily_backend_errors(monkeypatch):
    _patch_request(monkeypatch, lambda *a: None)
    with pytest.raises(RuntimeError, match="SSRF"):
        await s._TavilyBackend("k").search("q", 5)

    _patch_request(monkeypatch, lambda *a: {"status": 500, "text": ""})
    with pytest.raises(RuntimeError, match="HTTP 500"):
        await s._TavilyBackend("k").search("q", 5)

    _patch_request(monkeypatch, lambda *a: {"status": 200, "text": "not json"})
    with pytest.raises(RuntimeError, match="non-JSON"):
        await s._TavilyBackend("k").search("q", 5)


async def test_exa_backend_search(monkeypatch):
    payload = {"results": [{"title": "E", "url": "https://e.com", "text": "exatext"}]}
    _patch_request(monkeypatch, lambda *a: {"status": 200, "text": json.dumps(payload)})
    results = await s._ExaBackend("k").search("q", 3, ["e.com"], ["x.com"])
    assert results[0].url == "https://e.com"
    assert results[0].snippet == "exatext"


async def test_ddg_backend_search_success(monkeypatch):
    _patch_request(monkeypatch, lambda *a: {"status": 200, "text": DDG_HTML})
    results = await s._DDGBackend().search("q", 5)
    assert any(r.url == "https://example.com/page" for r in results)


async def test_ddg_backend_search_all_attempts_fail(monkeypatch):
    monkeypatch.setattr(s.asyncio, "sleep", _noop_sleep)
    _patch_request(monkeypatch, lambda *a: {"status": 503, "text": ""})
    with pytest.raises(RuntimeError, match="Search failed"):
        await s._DDGBackend().search("q", 5)


async def test_searxng_backend_search(monkeypatch):
    html = (
        '<article class="result">'
        '<a href="https://sx.example.com/a">SX</a>'
        '<p class="content">snip</p></article>'
    )
    _patch_request(monkeypatch, lambda *a: {"status": 200, "text": html})
    results = await s._SearXNGBackend().search("q", 5)
    assert results[0].url == "https://sx.example.com/a"


async def test_searxng_backend_all_fail(monkeypatch):
    _patch_request(monkeypatch, lambda *a: {"status": 502, "text": ""})
    with pytest.raises(RuntimeError, match="SearXNG"):
        await s._SearXNGBackend().search("q", 5)


async def _noop_sleep(*_a, **_k):
    return None
