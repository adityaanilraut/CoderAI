"""Tests for Web Search and Fetch Providers (Exa, Perplexity, DeepSeek, HTTP, and WebSearch tool)."""

from __future__ import annotations

import pytest
from coderai.core.web_providers import (
    ExaSearchProvider,
    PerplexitySearchProvider,
    DeepSeekSearchProvider,
    HttpSearchProvider,
    WebSearchSource,
    WebSearchResult,
    register_web_search_provider,
    resolve_web_search_provider,
    list_web_search_providers,
)
from coderai.core.tools.web_search import handle_web_search_tool
from coderai.core.tools.web_fetch import handle_web_fetch_tool


def test_provider_registry_listing():
    providers = list_web_search_providers()
    assert "exa" in providers
    assert "perplexity" in providers
    assert "deepseek" in providers
    assert "http" in providers


def test_resolve_default_provider():
    # When no API keys set, falls back to http
    p = resolve_web_search_provider()
    assert p is not None
    assert p.id in ("http", "exa", "perplexity", "deepseek")


def test_exa_provider_unavailable_without_key():
    p = ExaSearchProvider(api_key="")
    assert p.available() is False
    res = p.search("test query")
    assert "Exa API key not configured" in (res.error or "")


def test_perplexity_provider_unavailable_without_key():
    p = PerplexitySearchProvider(api_key="")
    assert p.available() is False
    res = p.search("test query")
    assert "Perplexity API key not configured" in (res.error or "")


@pytest.mark.asyncio
async def test_handle_web_search_tool_validation():
    # Missing queries
    r1 = await handle_web_search_tool({}, None)
    assert r1.ok is False
    assert "Missing required argument" in r1.error

    # Empty query string
    r2 = await handle_web_search_tool({"query": "   "}, None)
    assert r2.ok is False
    assert "No valid non-empty" in r2.error


@pytest.mark.asyncio
async def test_handle_web_search_mock_provider(monkeypatch):
    class MockProvider:
        id = "mock"
        def available(self):
            return True
        def search(self, q, max_results=8, timeout_seconds=15.0):
            return WebSearchResult(
                query=q,
                content="Summary of python 3.12 features",
                sources=[
                    WebSearchSource(
                        title="Python 3.12 Release Notes",
                        url="https://docs.python.org/3/whatsnew/3.12.html",
                        snippet="New type parameter syntax and more.",
                        published_at="2023-10-02",
                    )
                ],
            )

    register_web_search_provider(MockProvider())
    res = await handle_web_search_tool({"query": "python 3.12", "provider": "mock"}, None)
    assert res.ok is True
    assert "Python 3.12 Release Notes" in res.output
    assert "Summary of python 3.12 features" in res.output
    assert "https://docs.python.org" in res.output
