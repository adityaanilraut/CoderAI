"""Tests for the WebSearchTool."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestWebSearchToolSchema:
    """Parameter schema tests."""

    def test_parameters_schema(self):
        from coderAI.tools.web import WebSearchTool

        tool = WebSearchTool()
        params = tool.get_parameters()
        assert "query" in params["properties"]
        assert params["required"] == ["query"]

    def test_tool_name(self):
        from coderAI.tools.web import WebSearchTool

        tool = WebSearchTool()
        assert tool.name == "web_search"
        assert tool.description  # non-empty


class TestWebSearchExecution:
    """Execution tests with mocked HTTP."""

    @pytest.fixture(autouse=True)
    def _reset_session(self):
        """Ensure the module-level session is reset between tests."""
        import coderAI.tools.web as web_mod

        web_mod._web_session = None
        yield
        web_mod._web_session = None

    def test_empty_query_returns_no_results(self):
        from coderAI.tools.web import WebSearchTool

        tool = WebSearchTool()
        # WebSearchTool does not reject empty queries; it returns success
        # with no results instead.
        with patch("coderAI.tools.web.aiohttp.ClientSession") as MockSession:
            mock_resp = AsyncMock()
            mock_resp.status = 200
            mock_resp.json = AsyncMock(return_value={})
            mock_resp.text = AsyncMock(return_value="<html></html>")
            mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_resp.__aexit__ = AsyncMock(return_value=False)

            mock_ctx = MagicMock()
            mock_ctx.get = MagicMock(return_value=mock_resp)
            mock_ctx.post = MagicMock(return_value=mock_resp)

            mock_session = MagicMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            MockSession.return_value = mock_session

            result = asyncio.run(tool.execute(query=""))
            assert result["success"] is True
            assert result["results"] == []

    @patch("coderAI.tools.web._get_web_session")
    def test_successful_search(self, mock_get_session):
        from coderAI.tools.web import WebSearchTool

        # Build a fake aiohttp response
        html_body = (
            '<html><body>'
            '<a class="result__a" href="https://example.com">Example</a>'
            '<a class="result__snippet">A test snippet.</a>'
            '</body></html>'
        )

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.text = AsyncMock(return_value=html_body)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_get_session.return_value = mock_session

        tool = WebSearchTool()
        result = asyncio.run(tool.execute(query="test query"))
        assert result["success"] is True

    @patch("coderAI.tools.web.aiohttp.ClientSession")
    def test_network_error_returns_empty_results(self, MockSession):
        """When individual HTTP calls fail, inner methods return None and
        execute() falls through to the 'no results' branch (success=True)."""
        from coderAI.tools.web import WebSearchTool

        # Make the session context manager raise on __aenter__
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(side_effect=Exception("Connection refused"))
        mock_session.__aexit__ = AsyncMock(return_value=False)
        MockSession.return_value = mock_session

        tool = WebSearchTool()
        result = asyncio.run(tool.execute(query="test"))
        # Inner methods swallow the exception and return None,
        # so execute() returns the "no results" fallback
        assert result["success"] is True
        assert result["results"] == []

    def test_outer_exception_returns_error(self):
        """If the outer try/except in execute() catches, success=False."""
        from coderAI.tools.web import WebSearchTool

        tool = WebSearchTool()
        with patch.object(
            tool, "_search_instant_answer", side_effect=Exception("boom")
        ):
            with patch.object(
                tool, "_search_html", side_effect=Exception("boom")
            ):
                result = asyncio.run(tool.execute(query="test"))
                # The first exception propagates to the outer try/except
                assert result["success"] is False
                assert "error" in result
