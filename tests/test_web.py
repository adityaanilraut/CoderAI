"""Tests for the WebSearchTool."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


def _make_mock_session(html_body: str = "", status: int = 200, side_effect=None):
    """Create a mock aiohttp session that our shared-session code can use."""
    mock_resp = AsyncMock()
    mock_resp.status = status
    mock_resp.headers = {"Content-Type": "text/html"}
    mock_resp.read = AsyncMock(return_value=html_body.encode("utf-8"))
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    if side_effect:
        mock_session.request = AsyncMock(side_effect=side_effect)
    else:
        mock_session.request = AsyncMock(return_value=mock_resp)
    return mock_session


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
        with (
            patch("coderAI.tools.web._safe_request") as mock_safe_req,
            patch("coderAI.tools.web.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_safe_req.return_value = {
                "status": 200,
                "headers": {"Content-Type": "text/html"},
                "url": "https://example.com",
                "content_type": "text/html",
                "text": "<html></html>",
                "content": b"<html></html>",
            }
            result = asyncio.run(tool.execute(query=""))
            assert result["success"] is False

    @patch("coderAI.tools.web.asyncio.sleep", new_callable=AsyncMock)
    @patch("coderAI.tools.web._safe_request")
    def test_successful_search(self, mock_safe_req, mock_sleep):
        from coderAI.tools.web import WebSearchTool

        html_body = (
            "<html><body>"
            '<a href="https://example.com" class="result-link">Example</a>'
            '<a class="result-snippet">A test snippet.</a>'
            "</body></html>"
        )
        mock_safe_req.return_value = {
            "status": 200,
            "headers": {"Content-Type": "text/html"},
            "url": "https://html.duckduckgo.com/lite/",
            "content_type": "text/html",
            "text": html_body,
            "content": html_body.encode("utf-8"),
        }

        tool = WebSearchTool()
        result = asyncio.run(tool.execute(query="test query"))
        assert result["success"] is True

    @patch("coderAI.tools.web.asyncio.sleep", new_callable=AsyncMock)
    @patch("coderAI.tools.web._safe_request")
    def test_network_error_returns_empty_results(self, mock_safe_req, mock_sleep):
        from coderAI.tools.web import WebSearchTool

        mock_safe_req.side_effect = Exception("Connection refused")

        tool = WebSearchTool()
        result = asyncio.run(tool.execute(query="test"))
        assert result["success"] is False
        assert "error" in result

    def test_outer_exception_returns_error(self):
        from coderAI.tools.web import WebSearchTool

        tool = WebSearchTool()
        with patch.object(tool, "_search", side_effect=Exception("boom")):
            result = asyncio.run(tool.execute(query="test"))
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

        monkeypatch.setattr(web_mod, "_safe_request_cf", fake_safe_request)

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
                "content_type": "text/html",
                "text": "<html>Not Found</html>",
                "content": b"<html>Not Found</html>",
            }

        monkeypatch.setattr(web_mod, "_safe_request_cf", fake_safe_request)

        tool = DownloadFileTool()
        result = asyncio.run(
            tool.execute(url="https://example.com/nonexistent", destination_path="/tmp/test.bin")
        )
        assert result["success"] is False
