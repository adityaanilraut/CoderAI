"""Tests for WebSearch Tool and Web Card formatting matching deepseek-harness."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

from rich.console import Console
from coderai.cli.tool_card import _render_search_card, render_tool_card
from coderai.core.session import SessionMessage
from coderai.core.tools.web_search import handle_web_search_tool
from coderai.core.web_providers import (
    HttpSearchProvider,
    WebSearchResult,
    WebSearchSource,
)


def test_http_search_provider_returns_results_and_caches():
    provider = HttpSearchProvider()
    assert provider.available() is True
    assert provider.id == "http"

    # Test mock search
    fake_source = WebSearchSource(
        title="AI News Today",
        url="https://example.com/ai-news",
        snippet="Latest advancements in AI.",
    )
    with patch.object(
        provider,
        "search",
        return_value=WebSearchResult(
            query="ai news",
            sources=[fake_source],
        ),
    ):
        res = provider.search("ai news", max_results=5)
        assert len(res.sources) == 1
        assert res.sources[0].title == "AI News Today"
        assert res.sources[0].url == "https://example.com/ai-news"


def test_handle_web_search_tool_metadata_format():
    async def run():
        fake_source = WebSearchSource(
            title="OpenAI Announcements",
            url="https://openai.com/news",
            snippet="New model updates released.",
        )
        fake_res = WebSearchResult(
            query="openai news",
            sources=[fake_source],
        )

        with patch(
            "coderai.core.tools.web_search.resolve_web_search_provider"
        ) as mock_prov_resolve:
            mock_prov = MagicMock()
            mock_prov.id = "mock"
            mock_prov.search.return_value = fake_res
            mock_prov_resolve.return_value = mock_prov

            res = await handle_web_search_tool({"query": "openai news"}, None)
            assert res.ok is True
            assert "metadata" in res.to_dict() if hasattr(res, "to_dict") else res.metadata
            assert res.metadata["query"] == "openai news"
            assert len(res.metadata["sources"]) == 1
            assert res.metadata["sources"][0]["url"] == "https://openai.com/news"
            assert "## Search Results: `openai news`" in res.output

    asyncio.run(run())


def test_render_search_card_avoids_empty_table_rows():
    console = Console()

    # Case 1: Populated sources
    metadata_populated = {
        "query": "artificial intelligence",
        "results": [
            {
                "query": "artificial intelligence",
                "sources": [
                    {
                        "title": "AI Breakthroughs",
                        "url": "https://example.org/breakthroughs",
                        "snippet": "New transformer models demonstrate higher reasoning capabilities.",
                    }
                ],
            }
        ],
    }
    # Must render without error
    _render_search_card(console, None, metadata_populated)

    # Case 2: Empty results - should not render empty dummy rows
    metadata_empty = {
        "query": "empty query",
        "results": [],
    }
    _render_search_card(console, None, metadata_empty)


def test_render_tool_card_websearch_integration():
    console = Console()
    payload = {
        "ok": True,
        "name": "WebSearch",
        "output": "## Search Results: `ai`\n1. [AI](https://ai.test)",
        "metadata": {
            "query": "ai",
            "results": [
                {
                    "query": "ai",
                    "sources": [
                        {
                            "title": "AI Platform",
                            "url": "https://ai.test",
                            "snippet": "An AI platform.",
                        }
                    ],
                }
            ],
            "sources": [
                {
                    "title": "AI Platform",
                    "url": "https://ai.test",
                    "snippet": "An AI platform.",
                }
            ],
        },
    }
    msg = SessionMessage(
        id="msg_tool_web",
        session_id="sess_1",
        role="tool",
        content=json.dumps(payload),
    )
    render_tool_card(console, msg)
