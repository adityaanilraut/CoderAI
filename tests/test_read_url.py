"""Tests for ReadURLTool — SSRF protection and execution paths."""

import asyncio
import pytest
from unittest.mock import AsyncMock

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
        calls = []
        from coderAI.tools import web as web_mod

        async def capturing_safe_request(method, url, **kwargs):
            calls.append(url)
            return None  # pretend the request was blocked downstream

        monkeypatch.setattr(web_mod, "_safe_request", capturing_safe_request)
        asyncio.run(self.tool.execute(url="example.com/page"))
        assert calls and calls[0].startswith("https://")

    def test_allows_local_urls_with_env_var(self, monkeypatch):
        """CODERAI_ALLOW_LOCAL_URLS=1 bypasses SSRF check (may still fail to connect)."""
        monkeypatch.setenv("CODERAI_ALLOW_LOCAL_URLS", "1")
        result = asyncio.run(self.tool.execute(url="http://localhost:19999/notexist"))
        if not result["success"]:
            assert "SSRF" not in result.get("error", "")


class TestReadURLToolExecution:
    """Stub the request layer via ``_safe_request`` to exercise parsing."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.tool = ReadURLTool()

    def _stub_safe_request(self, monkeypatch, *, status=200, content_type="text/html",
                           text="<html><body>Hello world</body></html>",
                           final_url="https://example.com/"):
        from coderAI.tools import web as web_mod

        async def fake(method, url, **kwargs):
            return {
                "status": status,
                "headers": {"Content-Type": content_type},
                "url": final_url,
                "content_type": content_type,
                "text": text,
                "content": text.encode("utf-8"),
            }

        monkeypatch.setattr(web_mod, "_safe_request", fake)

    def test_success_returns_content(self, monkeypatch):
        self._stub_safe_request(monkeypatch)
        result = asyncio.run(self.tool.execute(url="https://example.com"))
        assert result["success"]
        assert "Hello world" in result["content"]

    def test_http_error_returns_failure(self, monkeypatch):
        self._stub_safe_request(monkeypatch, status=404, text="")
        result = asyncio.run(self.tool.execute(url="https://example.com/404"))
        assert not result["success"]
        assert "404" in result["error"]

    def test_truncation(self, monkeypatch):
        long_body = "<html><body>" + ("x" * 20000) + "</body></html>"
        self._stub_safe_request(monkeypatch, text=long_body)
        result = asyncio.run(self.tool.execute(url="https://example.com", max_length=100))
        assert result["success"]
        assert result["truncated"] is True
        assert len(result["content"]) <= 100

    def test_plain_text_not_html_parsed(self, monkeypatch):
        self._stub_safe_request(
            monkeypatch,
            content_type="text/plain",
            text="plain text content",
        )
        result = asyncio.run(self.tool.execute(url="https://example.com/file.txt"))
        assert result["success"]
        assert "plain text content" in result["content"]


# Mark AsyncMock as intentionally unused — retained for future tests that
# need it, e.g. streaming responses.
_ = AsyncMock
