"""Tests for the WebSearchTool."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch



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

    def test_empty_query_returns_no_results(self):
        from coderAI.tools.web import WebSearchTool

        tool = WebSearchTool()
        with patch("coderAI.tools.web.aiohttp.ClientSession") as MockSession, \
             patch("coderAI.tools.web.asyncio.sleep", new_callable=AsyncMock):
            mock_resp = AsyncMock()
            mock_resp.status = 200
            mock_resp.headers = {}
            mock_resp.read = AsyncMock(return_value=b"<html></html>")
            mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_resp.__aexit__ = AsyncMock(return_value=False)

            mock_ctx = MagicMock()
            mock_ctx.request = MagicMock(return_value=mock_resp)

            mock_session = MagicMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            MockSession.return_value = mock_session

            result = asyncio.run(tool.execute(query=""))
            assert result["success"] is False

    @patch("coderAI.tools.web.asyncio.sleep", new_callable=AsyncMock)
    @patch("coderAI.tools.web.aiohttp.ClientSession")
    def test_successful_search(self, MockSession, mock_sleep):
        from coderAI.tools.web import WebSearchTool

        html_body = (
            '<html><body>'
            '<a href="https://example.com" class="result-link">Example</a>'
            '<a class="result-snippet">A test snippet.</a>'
            '</body></html>'
        )

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.headers = {}
        mock_resp.read = AsyncMock(return_value=html_body.encode("utf-8"))
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_ctx = MagicMock()
        mock_ctx.request = MagicMock(return_value=mock_resp)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        MockSession.return_value = mock_session

        tool = WebSearchTool()
        result = asyncio.run(tool.execute(query="test query"))
        assert result["success"] is True

    @patch("coderAI.tools.web.asyncio.sleep", new_callable=AsyncMock)
    @patch("coderAI.tools.web.aiohttp.ClientSession")
    def test_network_error_returns_empty_results(self, MockSession, mock_sleep):
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
        assert result["success"] is False
        assert "Search failed" in result["error"]

    def test_outer_exception_returns_error(self):
        """If the outer try/except in execute() catches, success=False."""
        from coderAI.tools.web import WebSearchTool

        tool = WebSearchTool()
        with patch.object(
            tool, "_search", side_effect=Exception("boom")
        ):
            result = asyncio.run(tool.execute(query="test"))
            # The exception propagates to the outer try/except
            assert result["success"] is False
            assert "error" in result


class TestDownloadFileToolSchema:
    def test_parameters_schema(self):
        from coderAI.tools.web import DownloadFileTool

        tool = DownloadFileTool()
        params = tool.get_parameters()
        assert "url" in params["properties"]
        assert "destination_path" in params["properties"]
        assert params["required"] == ["url", "destination_path"]

    def test_tool_name(self):
        from coderAI.tools.web import DownloadFileTool

        tool = DownloadFileTool()
        assert tool.name == "download_file"
        assert tool.description


class TestDownloadFileExecution:
    def test_successful_download(self, tmp_path, monkeypatch):
        from coderAI.tools.web import DownloadFileTool
        from coderAI.tools import web as web_mod
        import os

        binary_data = b"fake binary data for testing"

        async def fake_safe_request(method, url, **kwargs):
            return {
                "status": 200,
                "headers": {"Content-Type": "application/octet-stream"},
                "url": url,
                "content_type": "application/octet-stream",
                "text": "",
                "content": binary_data,
            }

        monkeypatch.setattr(web_mod, "_safe_request", fake_safe_request)

        tool = DownloadFileTool()
        dest_file = tmp_path / "test_download.bin"

        result = asyncio.run(
            tool.execute(url="https://example.com/test.bin", destination_path=str(dest_file))
        )

        assert result["success"] is True
        assert result["bytes_downloaded"] == len(binary_data)
        assert result["destination_path"] == str(dest_file)

        assert os.path.exists(dest_file)
        with open(dest_file, "rb") as f:
            assert f.read() == binary_data

    def test_http_error(self, monkeypatch):
        from coderAI.tools.web import DownloadFileTool
        from coderAI.tools import web as web_mod

        async def fake_safe_request(method, url, **kwargs):
            return {
                "status": 404,
                "headers": {},
                "url": url,
                "content_type": "",
                "text": "",
                "content": b"",
            }

        monkeypatch.setattr(web_mod, "_safe_request", fake_safe_request)

        tool = DownloadFileTool()
        result = asyncio.run(tool.execute(url="https://example.com/notfound.bin", destination_path="notfound.bin"))

        assert result["success"] is False
        assert "HTTP 404" in result["error"]

