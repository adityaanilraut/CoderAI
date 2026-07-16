"""Focused browser network and screenshot trust-boundary regressions."""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coderAI.types.provenance import Provenance
from coderAI.core.services import ToolServices, services_scope
from coderAI.system.config import Config
from coderAI.system import fsperms
from coderAI.tools import browser
from coderAI.tools.browser import (
    BrowserClickTool,
    BrowserEvaluateTool,
    BrowserGetContentTool,
    BrowserNavigateTool,
    BrowserScreenshotTool,
    BrowserSelectOptionTool,
    BrowserSession,
    BrowserSnapshotTool,
    BrowserTypeTool,
    BrowserWaitTool,
)


@pytest.mark.asyncio
async def test_context_interception_is_installed_before_page_use() -> None:
    events: list[str] = []
    context = MagicMock()
    context.route = AsyncMock(side_effect=lambda *_args: events.append("route"))
    context.route_web_socket = AsyncMock(side_effect=lambda *_args: events.append("websocket"))
    context.add_init_script = AsyncMock(side_effect=lambda **_kwargs: events.append("script"))
    context.new_page = AsyncMock(side_effect=lambda: events.append("page") or MagicMock())
    launched_browser = MagicMock()
    launched_browser.new_context = AsyncMock(return_value=context)
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch=AsyncMock(return_value=launched_browser))
    )
    manager = MagicMock()
    manager.start = AsyncMock(return_value=playwright)

    session = BrowserSession("test")
    with patch.object(browser, "_playwright_manager", return_value=manager):
        await session._ensure_browser()

    assert events == ["route", "websocket", "script", "page"]
    assert launched_browser.new_context.await_args.kwargs["service_workers"] == "block"
    assert context.route.await_args.args[0] == "**/*"


@pytest.mark.asyncio
async def test_context_route_blocks_private_subresource_and_retains_reason() -> None:
    session = BrowserSession("test")
    route = MagicMock()
    route.abort = AsyncMock()
    route.continue_ = AsyncMock()
    request = SimpleNamespace(url="http://169.254.169.254/latest/meta-data/")

    await session._intercept_request(route, request)

    route.abort.assert_awaited_once_with("blockedbyclient")
    route.continue_.assert_not_awaited()
    assert session._blocked_request_reason is not None
    assert "SSRF guard" in session._blocked_request_reason


@pytest.mark.asyncio
async def test_context_route_validates_and_continues_public_fetch() -> None:
    session = BrowserSession("test")
    route = MagicMock()
    route.abort = AsyncMock()
    route.continue_ = AsyncMock()
    request = SimpleNamespace(url="https://cdn.example.test/app.js")

    with patch.object(browser, "_resolve_host_addrs", AsyncMock(return_value=["93.184.216.34"])):
        await session._intercept_request(route, request)

    route.continue_.assert_awaited_once_with()
    route.abort.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_validation_exception_fails_closed() -> None:
    session = BrowserSession("test")
    route = MagicMock()
    route.abort = AsyncMock()
    route.continue_ = AsyncMock()

    with patch.object(
        browser, "_validate_navigation_url", AsyncMock(side_effect=RuntimeError("resolver failed"))
    ):
        await session._intercept_request(route, SimpleNamespace(url="https://example.test/"))

    route.abort.assert_awaited_once_with("blockedbyclient")
    assert "resolver failed" in (session._blocked_request_reason or "")


@pytest.mark.asyncio
async def test_navigation_returns_interceptor_block_reason() -> None:
    session = BrowserSession("test")
    session._ensure_browser = AsyncMock()  # type: ignore[method-assign]
    route = MagicMock()
    route.abort = AsyncMock()
    route.continue_ = AsyncMock()
    page = MagicMock()

    async def blocked_goto(*_args: object, **_kwargs: object) -> None:
        await session._intercept_request(route, SimpleNamespace(url="http://127.0.0.1/private"))
        raise RuntimeError("net::ERR_BLOCKED_BY_CLIENT")

    page.goto = AsyncMock(side_effect=blocked_goto)
    session._page = page

    result = await session.navigate("https://public.example.test/")

    assert result["success"] is False
    assert "SSRF guard" in result["error"]
    assert "127.0.0.1" in result["error"]


NETWORK_BROWSER_TOOLS = (
    BrowserNavigateTool,
    BrowserClickTool,
    BrowserTypeTool,
    BrowserSelectOptionTool,
    BrowserEvaluateTool,
)

BROWSER_OUTPUT_TOOLS = NETWORK_BROWSER_TOOLS + (
    BrowserSnapshotTool,
    BrowserGetContentTool,
    BrowserScreenshotTool,
    BrowserWaitTool,
)


@pytest.mark.parametrize("tool_cls", NETWORK_BROWSER_TOOLS)
def test_browser_network_tools_are_egress(tool_cls: type) -> None:
    assert tool_cls().is_egress is True


@pytest.mark.parametrize("tool_cls", BROWSER_OUTPUT_TOOLS)
def test_browser_derived_outputs_are_untrusted(tool_cls: type) -> None:
    assert tool_cls().result_provenance == Provenance.UNTRUSTED_EXTERNAL


def test_browser_screenshot_is_mutating_and_confirmed() -> None:
    tool = BrowserScreenshotTool()
    assert tool.is_read_only is False
    assert tool.requires_confirmation is True


@pytest.mark.asyncio
async def test_screenshot_rejects_project_traversal_before_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = BrowserSession("test")
    session._ensure_browser = AsyncMock()  # type: ignore[method-assign]
    page = MagicMock()
    page.screenshot = AsyncMock(return_value=b"png")
    session._page = page
    config = Config(project_root=str(tmp_path))
    monkeypatch.setenv("CODERAI_ALLOW_OUTSIDE_PROJECT", "1")

    with services_scope(ToolServices(config=config)):
        result = await session.screenshot("../outside.png")

    assert result["success"] is False
    assert "outside project root" in result["error"]
    page.screenshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_screenshot_writes_bytes_atomically_inside_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = BrowserSession("test")
    session._ensure_browser = AsyncMock()  # type: ignore[method-assign]
    page = MagicMock()
    page.screenshot = AsyncMock(return_value=b"png-bytes")
    session._page = page
    config = Config(project_root=str(tmp_path))
    target = tmp_path / "page.png"
    real_replace = os.replace
    replacements: list[tuple[Path, Path]] = []

    def observed_replace(source: str, destination: str | os.PathLike[str]) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        assert source_path.parent == tmp_path
        assert source_path.read_bytes() == b"png-bytes"
        replacements.append((source_path, destination_path))
        real_replace(source, destination)

    monkeypatch.setattr(fsperms.os, "replace", observed_replace)
    with services_scope(ToolServices(config=config)):
        result = await session.screenshot("page.png")

    assert result["success"] is True
    assert target.read_bytes() == b"png-bytes"
    assert len(replacements) == 1
    assert replacements[0][1] == target
    assert list(tmp_path.glob(".page.png.*.tmp")) == []


@pytest.mark.asyncio
async def test_screenshot_rejects_symlink_destination(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.png"
    outside.write_bytes(b"original")
    target = tmp_path / "page.png"
    try:
        target.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    session = BrowserSession("test")
    session._ensure_browser = AsyncMock()  # type: ignore[method-assign]
    page = MagicMock()
    page.screenshot = AsyncMock(return_value=b"replacement")
    session._page = page
    config = Config(project_root=str(tmp_path))

    with services_scope(ToolServices(config=config)):
        result = await session.screenshot(str(target))

    assert result["success"] is False
    assert "symlink leaf" in result["error"]
    assert outside.read_bytes() == b"original"
    page.screenshot.assert_not_awaited()
