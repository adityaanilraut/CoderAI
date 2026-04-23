"""Tests for ReadURLTool — SSRF protection and execution paths."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from coderAI.tools.web import ReadURLTool


class TestReadURLToolSSRF:
    """SSRF protection tests — no real network calls needed."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.tool = ReadURLTool()

    def test_blocks_localhost(self):
        result = asyncio.run(self.tool.execute(url="http://localhost/secret"))
        assert not result["success"]
        assert "SSRF" in result["error"] or "Blocked" in result["error"]

    def test_blocks_127_0_0_1(self):
        result = asyncio.run(self.tool.execute(url="http://127.0.0.1/admin"))
        assert not result["success"]

    def test_blocks_private_ip_range(self):
        result = asyncio.run(self.tool.execute(url="http://192.168.1.1/router"))
        assert not result["success"]

    def test_blocks_internal_10_range(self):
        result = asyncio.run(self.tool.execute(url="http://10.0.0.1/internal"))
        assert not result["success"]

    def test_prepends_https_when_missing(self, monkeypatch):
        """Bare domain without scheme should be prefixed with https://."""
        # Block at SSRF level so no real request is sent; just verify URL is rewritten
        calls = []
        from coderAI.tools import web as web_mod
        def capturing_check(url):
            calls.append(url)
            return False  # block so no real request
        monkeypatch.setattr(web_mod, "_is_safe_url", capturing_check)
        asyncio.run(self.tool.execute(url="example.com/page"))
        assert calls and calls[0].startswith("https://")

    def test_allows_local_urls_with_env_var(self, monkeypatch, tmp_path):
        """CODERAI_ALLOW_LOCAL_URLS=1 bypasses SSRF check (still may fail to connect)."""
        monkeypatch.setenv("CODERAI_ALLOW_LOCAL_URLS", "1")
        # We just verify it doesn't return an SSRF block error — it may fail to connect
        result = asyncio.run(self.tool.execute(url="http://localhost:19999/notexist"))
        # Could be connection error, but NOT an SSRF block
        if not result["success"]:
            assert "SSRF" not in result.get("error", "")


class TestReadURLToolExecution:
    """Mock the HTTP layer to test the parsing/truncation logic."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.tool = ReadURLTool()

    def _mock_response(self, status=200, content_type="text/html", body="<html><body>Hello world</body></html>"):
        mock_resp = AsyncMock()
        mock_resp.status = status
        mock_resp.headers = {"Content-Type": content_type}
        mock_resp.url = "https://example.com/"
        mock_resp.text = AsyncMock(return_value=body)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        return mock_resp

    def _patch_session(self, mock_resp):
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        return mock_session

    def test_success_returns_content(self, monkeypatch):
        from coderAI.tools import web as web_mod
        monkeypatch.setattr(web_mod, "_is_safe_url", lambda url: True)
        mock_resp = self._mock_response(body="<html><body>Hello world</body></html>")
        mock_session = self._patch_session(mock_resp)
        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = asyncio.run(self.tool.execute(url="https://example.com"))
        assert result["success"]
        assert "Hello world" in result["content"]

    def test_http_error_returns_failure(self, monkeypatch):
        from coderAI.tools import web as web_mod
        monkeypatch.setattr(web_mod, "_is_safe_url", lambda url: True)
        mock_resp = self._mock_response(status=404)
        mock_session = self._patch_session(mock_resp)
        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = asyncio.run(self.tool.execute(url="https://example.com/404"))
        assert not result["success"]
        assert "404" in result["error"]

    def test_truncation(self, monkeypatch):
        from coderAI.tools import web as web_mod
        monkeypatch.setattr(web_mod, "_is_safe_url", lambda url: True)
        long_body = "<html><body>" + ("x" * 20000) + "</body></html>"
        mock_resp = self._mock_response(body=long_body)
        mock_session = self._patch_session(mock_resp)
        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = asyncio.run(self.tool.execute(url="https://example.com", max_length=100))
        assert result["success"]
        assert result["truncated"] is True
        assert len(result["content"]) <= 100

    def test_plain_text_not_html_parsed(self, monkeypatch):
        from coderAI.tools import web as web_mod
        monkeypatch.setattr(web_mod, "_is_safe_url", lambda url: True)
        mock_resp = self._mock_response(
            content_type="text/plain",
            body="plain text content"
        )
        mock_session = self._patch_session(mock_resp)
        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = asyncio.run(self.tool.execute(url="https://example.com/file.txt"))
        assert result["success"]
        assert "plain text content" in result["content"]
