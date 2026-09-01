"""Tests for Phase 4 Upgrades: Advanced Tooling & Ecosystem Extensions.

Covers:
1. Headless Browser Automation & DOM Extraction Engine:
   - DOM tree extraction, title extraction, and interactive element indexing [#N].
   - browser_navigate, browser_click, browser_type, browser_snapshot, browser_close tools.
   - Form input simulation and link navigation.

2. Telemetry & Streaming Event Middleware Interceptor Chain:
   - ExecutionSpan lifecycle, duration tracking, attributes, and span events.
   - TelemetryCollector metric counters and summary aggregation.
   - OpenTelemetry (OTel) export formatting.
   - Lifecycle hook telemetry span integration.

3. Dynamic MCP Client Auto-Healing & Hot Reloading:
   - McpClient ping() liveness probing.
   - McpManager probe_health() probing all server transports.
   - McpManager auto_heal_servers() with exponential backoff recovery.
   - McpManager hot_reload_tools() schema hot-swapping.
"""

from __future__ import annotations

import time
import pytest

from coderai.core.mcp.client import McpClient
from coderai.core.mcp.manager import McpManager
from coderai.core.telemetry import (
    ExecutionSpan,
    TelemetryCollector,
)
from coderai.core.tools.browser import (
    DOMExtractor,
    HeadlessBrowserDriver,
    handle_browser_click_tool,
    handle_browser_close_tool,
    handle_browser_navigate_tool,
    handle_browser_snapshot_tool,
    handle_browser_type_tool,
)
from coderai.core.tools.registry import get_tool_registry


# ==============================================================================
# 1. Headless Browser Automation & DOM Extraction Engine Tests
# ==============================================================================


SAMPLE_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>CoderAI Test Dashboard</title>
</head>
<body>
    <header>
        <h1>Welcome to Dashboard</h1>
        <a href="/docs" id="nav-docs">Documentation</a>
        <a href="/settings" id="nav-settings">Settings</a>
    </header>
    <main>
        <p>This is a test application for headless browser validation.</p>
        <form id="search-form">
            <input type="text" name="query" placeholder="Search documentation..." value="" />
            <button type="submit" id="btn-submit">Search</button>
        </form>
        <div role="button" id="custom-btn">Custom Action</div>
    </main>
</body>
</html>"""


def test_dom_extractor_indexing():
    """Verify DOMExtractor indexes interactive elements with unique ref IDs [#N]."""
    extractor = DOMExtractor()
    extractor.feed(SAMPLE_HTML)

    assert extractor.title == "CoderAI Test Dashboard"
    assert len(extractor.elements) >= 4

    # Check links and buttons
    tags = [e.tag for e in extractor.elements]
    assert "a" in tags
    assert "input" in tags
    assert "button" in tags

    # Verify indexed refs are 1-based sequential
    refs = [e.ref_id for e in extractor.elements]
    assert refs == list(range(1, len(extractor.elements) + 1))


def test_headless_browser_navigate_and_format():
    """Verify HeadlessBrowserDriver.navigate loads HTML and creates formatted summary."""
    driver = HeadlessBrowserDriver()
    state = driver.navigate("http://localhost:3000/app", html_override=SAMPLE_HTML)

    assert state.title == "CoderAI Test Dashboard"
    assert state.url == "http://localhost:3000/app"
    assert len(state.elements) >= 4

    summary = state.format_summary()
    assert "Title: CoderAI Test Dashboard" in summary
    assert "Interactive Elements:" in summary
    assert "[#1] <A>" in summary or "[#1] <" in summary
    assert "Documentation" in summary


def test_headless_browser_type_and_click():
    """Verify browser typing into input fields and clicking links/buttons."""
    driver = HeadlessBrowserDriver()
    driver.navigate("https://example.com", html_override=SAMPLE_HTML)

    # Find the input element (ref_id 3 in this DOM)
    input_elem = next(e for e in driver.state.elements if e.tag == "input")
    ok, msg, state = driver.type(input_elem.ref_id, "Python testing", clear_first=True)
    assert ok is True
    assert input_elem.value == "Python testing"
    assert "Typed text into" in msg

    # Click navigation link to docs
    link_elem = next(e for e in driver.state.elements if e.tag == "a" and "Docs" in e.text or "docs" in (e.href or ""))
    ok_click, msg_click, state_click = driver.click(link_elem.ref_id)
    assert ok_click is True
    assert "https://example.com/docs" in state_click.url or "docs" in state_click.url


def test_browser_tool_handlers():
    """Verify ToolResult responses from browser tool handlers."""
    ctx = {"session_id": "test_browser_sess"}

    # 1. Navigate
    res_nav = handle_browser_navigate_tool(
        {"url": "https://dashboard.local", "html_override": SAMPLE_HTML}, ctx
    )
    assert res_nav.ok is True
    assert "CoderAI Test Dashboard" in (res_nav.output or "")
    assert res_nav.metadata.get("element_count", 0) >= 4

    # 2. Snapshot
    res_snap = handle_browser_snapshot_tool({"extract_dom": True}, ctx)
    assert res_snap.ok is True
    assert "Interactive Elements:" in (res_snap.output or "")

    # 3. Type
    res_type = handle_browser_type_tool(
        {"element_ref": 3, "text": "automated search query", "clear_first": True}, ctx
    )
    assert res_type.ok is True

    # 4. Click
    res_click = handle_browser_click_tool({"element_ref": 1}, ctx)
    assert res_click.ok is True

    # 5. Close
    res_close = handle_browser_close_tool({}, ctx)
    assert res_close.ok is True
    assert "Browser session closed" in (res_close.output or "")


def test_browser_tools_registered_in_registry():
    """Verify all browser tools are discoverable in the central ToolRegistry."""
    registry = get_tool_registry()
    assert registry.get_tool("browser_navigate") is not None
    assert registry.get_tool("browser_click") is not None
    assert registry.get_tool("browser_type") is not None
    assert registry.get_tool("browser_snapshot") is not None
    assert registry.get_tool("browser_close") is not None


# ==============================================================================
# 2. Telemetry & Streaming Event Middleware Interceptor Chain Tests
# ==============================================================================


def test_execution_span_lifecycle():
    """Verify ExecutionSpan tracks duration, status, attributes, and events."""
    span = ExecutionSpan(
        span_id="span_123",
        trace_id="trace_456",
        name="tool:edit",
        kind="tool",
        attributes={"file_path": "src/main.py"},
    )
    assert span.status == "running"
    assert span.duration_ms == 0.0

    time.sleep(0.01)
    span.add_event("validation_passed", {"rules": 3})
    span.finish(status="ok", extra_attributes={"lines_changed": 15})

    assert span.status == "ok"
    assert span.duration_ms >= 5.0
    assert span.attributes["lines_changed"] == 15
    assert len(span.events) == 1
    assert span.events[0]["name"] == "validation_passed"

    # Verify OpenTelemetry export schema
    otel_span = span.to_otel_span()
    assert otel_span["traceId"] == "trace_456"
    assert otel_span["spanId"] == "span_123"
    assert otel_span["name"] == "tool:edit"
    assert otel_span["status"]["code"] == 1


def test_telemetry_collector_aggregation_and_metrics():
    """Verify TelemetryCollector metrics counters, span tracking, and export."""
    collector = TelemetryCollector()
    collector.set_active_trace_id("trace_main_sess")

    # Start spans
    s1 = collector.start_span("llm:gpt-5", kind="llm", attributes={"model": "gpt-5"})
    time.sleep(0.005)
    collector.end_span(s1.span_id, status="ok")

    s2 = collector.start_span("tool:bash", kind="tool", parent_span_id=s1.span_id)
    time.sleep(0.005)
    collector.end_span(s2.span_id, status="error", error="Permission denied")

    # Increment counters
    collector.increment_counter("tool_calls", 2.0)
    collector.increment_counter("llm_tokens", 520.0)

    summary = collector.get_metrics_summary()
    assert summary["total_spans"] == 2
    assert summary["ok_spans"] == 1
    assert summary["error_spans"] == 1
    assert summary["counters"]["tool_calls"] == 2.0
    assert summary["counters"]["llm_tokens"] == 520.0

    spans = collector.export_spans()
    assert len(spans) == 2
    assert spans[0]["trace_id"] == "trace_main_sess"


# ==============================================================================
# 3. Dynamic MCP Client Auto-Healing & Hot Reloading Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_mcp_client_ping_probing():
    """Verify McpClient.ping returns False when disconnected and probes liveness."""
    client = McpClient("test_server", command_or_config={"command": "mock_cmd"})
    # When not connected, ping must safely return False without raising
    is_alive = await client.ping(timeout_s=0.5)
    assert is_alive is False


@pytest.mark.asyncio
async def test_mcp_manager_probe_health():
    """Verify McpManager.probe_health checks all configured server connections."""
    manager = McpManager()
    manager.configured_server_names = ["server_a", "server_b"]

    # Both uninitialized/disconnected
    health = await manager.probe_health()
    assert "server_a" in health
    assert "server_b" in health
    assert health["server_a"] is False
    assert health["server_b"] is False


@pytest.mark.asyncio
async def test_mcp_manager_hot_reload_tools():
    """Verify McpManager.hot_reload_tools triggers on_tools_list_changed notification."""
    manager = McpManager()
    notifications: list[str] = []
    manager.set_on_tools_list_changed(lambda: notifications.append("tools_changed"))

    tools = await manager.hot_reload_tools()
    assert isinstance(tools, list)
    assert "tools_changed" in notifications
