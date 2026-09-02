"""Tests for Second-Pass Parity & Hardening Audit against DeepSeek Harness reference.

Validates:
1. RepeatToolReminder reset capability and user-turn chain clearing.
2. Telemetry event classification in LOG_ONLY_EVENT_TYPES.
3. SubAgentManager timeout enforcement on external provider backends.
4. ToolExecutor MCP tool timeout enforcement.
5. Session reference canonical URI and slash-delimited mention parsing.
6. Hierarchical project instructions and modular rules discovery.
"""

from __future__ import annotations

import asyncio
import base64
import json
import pathlib
import tempfile
import pytest
from unittest.mock import MagicMock, patch

from coderai.core.common.repeat_tool_reminder import RepeatToolReminder
from coderai.core.common.session_reference import (
    extract_session_reference_ids,
)
from coderai.core.events import (
    LOG_ONLY_EVENT_TYPES,
    TELEMETRY_SPAN_START,
    TELEMETRY_SPAN_END,
    TELEMETRY_METRIC,
    SessionEvent,
)
from coderai.core.prompt import load_agent_instructions
from coderai.core.subagent import SubAgentManager, SubAgentSpec
from coderai.core.tools.executor import ToolExecutor
from coderai.core.tools.types import ToolExecutionHooks


# --- 1. RepeatToolReminder Reset Tests ---


def test_repeat_tool_reminder_reset():
    """Verify reset() clears tracking chain so repeated calls restart from 1."""
    reminder = RepeatToolReminder(thresholds=(2, 4))
    assert reminder.observe("read", {"file_path": "a.txt"}) is None
    assert reminder.observe("read", {"file_path": "a.txt"}) is not None

    reminder.reset()
    assert reminder._key is None
    assert reminder._count == 0
    assert reminder.observe("read", {"file_path": "a.txt"}) is None


# --- 2. Telemetry Event Log-Only Classification Tests ---


def test_telemetry_events_in_log_only():
    """Verify telemetry events are strictly log-only and never surface into message history."""
    assert TELEMETRY_SPAN_START in LOG_ONLY_EVENT_TYPES
    assert TELEMETRY_SPAN_END in LOG_ONLY_EVENT_TYPES
    assert TELEMETRY_METRIC in LOG_ONLY_EVENT_TYPES

    ev = SessionEvent(
        seq=1, time=1000.0, type=TELEMETRY_METRIC, data={"name": "token_count", "value": 42}
    )
    assert ev.is_log_only
    assert not ev.is_surface


# --- 3. Subagent Provider Timeout Enforcement Tests ---


@pytest.mark.asyncio
async def test_subagent_out_of_process_timeout():
    """Verify out-of-process provider drivers enforce spec.timeout_seconds."""
    manager = SubAgentManager(
        project_root="/tmp",
        create_openai_client=lambda: {"client": MagicMock()},
    )

    spec = SubAgentSpec(
        description="Slow task",
        prompt="Execute slow operation",
        provider="claude_code",
        timeout_seconds=0.05,
    )

    async def slow_execute(*args, **kwargs):
        await asyncio.sleep(0.5)
        return {"ok": True, "summary": "Done"}

    with patch(
        "coderai.core.subagent_backends.claude_code.ClaudeCodeDriver.execute",
        side_effect=slow_execute,
    ):
        result = await manager.spawn_subagent(spec)
        assert result.status == "timeout"
        assert "TimeoutError" in (result.error or "")


# --- 4. MCP Tool Execution Timeout Tests ---


@pytest.mark.asyncio
async def test_mcp_tool_timeout_enforcement():
    """Verify _run_mcp in ToolExecutor enforces timeout_ms from hooks."""
    mock_mcp = MagicMock()

    async def slow_mcp(*args, **kwargs):
        await asyncio.sleep(0.5)
        return {"ok": True, "output": "finished"}

    mock_mcp.execute_mcp_tool = slow_mcp
    mock_mcp.is_mcp_tool = lambda name: True

    executor = ToolExecutor(project_root="/tmp", mcp_manager=mock_mcp)
    hooks = ToolExecutionHooks(timeout_ms=50)

    result = await executor.execute_tool_call(
        session_id="ses_test",
        tool_call={
            "id": "call_1",
            "type": "function",
            "function": {"name": "mcp_slow", "arguments": "{}"},
        },
        hooks=hooks,
    )

    assert not result.ok
    assert "TOOL_TIMEOUT" in (result.error or "")


# --- 5. Session Reference URI & Mention Tests ---


def test_session_reference_uri_and_mention_parsing():
    """Verify session reference extraction parses @session:, @session/, and canonical dsh-session: URIs."""
    sid = "target_session_99"
    b64 = base64.urlsafe_b64encode(json.dumps(sid).encode("utf-8")).decode("ascii").rstrip("=")
    uri = f"dsh-session:{b64}"

    text = f"Check @session:ses_01, also @session/ses_02 and @[Prior Run]({uri}) as well as raw session:longsessionid123"
    ids = extract_session_reference_ids(text)

    assert "ses_01" in ids
    assert "ses_02" in ids
    assert sid in ids
    assert "longsessionid123" in ids


# --- 6. Hierarchical Instructions & Modular Rules Tests ---


def test_hierarchical_rules_discovery():
    """Verify load_agent_instructions discovers primary AGENTS.md plus .coderai/rules/ and .agents/rules/."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = pathlib.Path(tmpdir)
        (root / "AGENTS.md").write_text("Primary repo guidance", encoding="utf-8")

        rules_dir = root / ".coderai" / "rules"
        rules_dir.mkdir(parents=True)
        (rules_dir / "security.md").write_text("Rule 1: No plain secrets", encoding="utf-8")
        (rules_dir / "testing.md").write_text(
            "Rule 2: Write tests for all bugfixes", encoding="utf-8"
        )

        inst = load_agent_instructions(str(root))
        assert inst is not None
        assert "Primary repo guidance" in inst
        assert "Rule 1: No plain secrets" in inst
        assert "Rule 2: Write tests for all bugfixes" in inst
