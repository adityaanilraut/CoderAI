"""Tests for Phase 2 Core Capability Expansion upgrades:
1. Dynamic MCP Server Ejection & Tool Registry Overrides (Gap 8.1)
2. Session Fork & Branch Tree Navigation (Gap 6.1)
3. Dual-Trigger Compaction & Shadow Events (Gap 6.3)
4. Adaptive Context Budgeting (Gap 3.1)
"""

from __future__ import annotations

import json
import pathlib
import uuid
import pytest

from coderai.core.compaction import (
    BasicCompaction,
    evaluate_compaction_trigger,
)
from coderai.core.events import (
    COMPACTION_END,
    COMPACTION_START,
    COMPACTION_SUMMARY,
)
from coderai.core.mcp.client import McpClient
from coderai.core.mcp.manager import McpManager, McpToolEntry, McpServerStatus
from coderai.core.prompt import (
    calculate_context_budget,
    get_compact_prompt_token_threshold,
)
from coderai.core.session import SessionManager
from coderai.core.tools.executor import ToolExecutor
from coderai.core.tools.registry import ToolRegistry


# ===========================================================================
# 1. MCP Dynamic Ejection & Tool Registry Overrides (Gap 8.1)
# ===========================================================================


class DummyMcpClient(McpClient):
    def __init__(self, server_name: str) -> None:
        super().__init__(server_name, {"command": "dummy"})
        self._connected = True

    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def call_tool(self, name: str, args: dict, timeout_s: float = 60.0) -> dict:
        return {
            "content": [{"type": "text", "text": f"Executed {name} with {args}"}],
            "isError": False,
        }


@pytest.mark.asyncio
async def test_mcp_dynamic_ejection_and_reload() -> None:
    mgr = McpManager()
    client = DummyMcpClient("test_server")
    mgr.clients.append(client)
    mgr.configured_server_names.append("test_server")
    mgr.server_configs["test_server"] = {"command": "dummy"}
    mgr.server_statuses.append(McpServerStatus(name="test_server", status="ready", connected=True))
    mgr.tools.append(
        McpToolEntry(
            server_name="test_server",
            original_name="echo",
            namespaced_name="mcp__test_server__echo",
            definition={"name": "echo", "inputSchema": {"type": "object"}},
            client=client,
        )
    )

    tools_changed_called = False

    def on_tools_changed() -> None:
        nonlocal tools_changed_called
        tools_changed_called = True

    mgr.set_on_tools_list_changed(on_tools_changed)

    # Verify initial active server list
    assert "test_server" in mgr.list_active_servers()
    assert len(mgr.list_tools()) == 1
    assert mgr.is_mcp_tool("mcp__test_server__echo")

    # Eject server
    ejected = mgr.eject_server("test_server")
    assert ejected is True
    assert "test_server" not in mgr.list_active_servers()
    assert len(mgr.list_tools()) == 0
    assert not mgr.is_mcp_tool("mcp__test_server__echo")
    assert tools_changed_called is True

    # Ejecting non-existent server returns False
    assert mgr.eject_server("non_existent") is False


@pytest.mark.asyncio
async def test_mcp_session_tool_masking() -> None:
    mgr = McpManager()
    client = DummyMcpClient("server1")
    mgr.clients.append(client)
    tool_entry1 = McpToolEntry(
        server_name="server1",
        original_name="tool_a",
        namespaced_name="mcp__server1__tool_a",
        definition={"name": "tool_a", "inputSchema": {"type": "object"}},
        client=client,
    )
    tool_entry2 = McpToolEntry(
        server_name="server1",
        original_name="tool_b",
        namespaced_name="mcp__server1__tool_b",
        definition={"name": "tool_b", "inputSchema": {"type": "object"}},
        client=client,
    )
    mgr.tools.extend([tool_entry1, tool_entry2])

    session_id = "ses_test_mask"
    mgr.set_session_tool_mask(session_id, allow=["mcp__server1__tool_a"])

    # Verify session-scoped filtering
    assert mgr.is_tool_enabled_for_session(session_id, "mcp__server1__tool_a") is True
    assert mgr.is_tool_enabled_for_session(session_id, "mcp__server1__tool_b") is False

    session_tools = mgr.list_tools(session_id=session_id)
    assert len(session_tools) == 1
    assert session_tools[0].namespaced_name == "mcp__server1__tool_a"

    defs = mgr.get_mcp_tool_definitions(session_id=session_id)
    assert len(defs) == 1
    assert defs[0]["function"]["name"] == "mcp__server1__tool_a"

    # Execution check for masked tool
    res_denied = await mgr.execute_mcp_tool("mcp__server1__tool_b", {}, session_id=session_id)
    assert res_denied.ok is False
    assert "disabled" in res_denied.error.lower()

    # Clear mask
    mgr.clear_session_tool_mask(session_id)
    assert mgr.is_tool_enabled_for_session(session_id, "mcp__server1__tool_b") is True


@pytest.mark.asyncio
async def test_tool_registry_suppression_and_session_masks(tmp_path: pathlib.Path) -> None:
    registry = ToolRegistry()

    # Test tool suppression in registry
    assert registry.has_tool("read")
    disposer = registry.suppress_tool("read")
    assert not registry.has_tool("read")
    assert registry.is_tool_suppressed("read")

    # Restore tool
    disposer()
    assert registry.has_tool("read")
    assert not registry.is_tool_suppressed("read")

    # Test session-specific tool mask
    session_id = "ses_scoped_mask"
    registry.set_session_mask(session_id, allow=["read", "grep"])

    assert registry.has_tool("read", scope=session_id)
    assert registry.has_tool("grep", scope=session_id)
    assert not registry.has_tool("write", scope=session_id)
    assert not registry.has_tool("bash", scope=session_id)

    # Tool executor enforcement
    executor = ToolExecutor(str(tmp_path), registry=registry)
    tool_call = {
        "id": "tc_1",
        "function": {
            "name": "bash",
            "arguments": json.dumps({"command": "echo 1", "sideEffects": []}),
        },
    }
    res = await executor.execute_tool_call(session_id, tool_call)
    assert res.ok is False
    assert "disabled or masked" in res.error.lower()

    # Clear session mask
    registry.clear_session_mask(session_id)
    assert registry.has_tool("bash", scope=session_id)


# ===========================================================================
# 2. Session Fork & Branch Tree Navigation (Gap 6.1)
# ===========================================================================


@pytest.mark.asyncio
async def test_session_fork_and_branch_tree(tmp_path: pathlib.Path) -> None:
    manager = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": None},
        get_resolved_settings=lambda: {"model": "gpt-5.6-luna"},
    )

    parent_id = str(uuid.uuid4().hex)
    now = "2026-08-21T00:00:00Z"
    entry = {
        "id": parent_id,
        "summary": "Original Branch",
        "assistantReply": "Parent Reply",
        "status": "completed",
        "createTime": now,
        "updateTime": now,
        "activeTokens": 1500,
        "planMode": False,
    }
    index = {"version": 1, "entries": [entry], "originalPath": str(tmp_path)}
    manager._save_index(index)

    # Write multiple messages to parent
    msg1 = manager._build_message(parent_id, "user", "Message 1", meta={"checkpointHash": "hash1"})
    msg1.id = "msg_1"
    msg2 = manager._build_message(parent_id, "assistant", "Message 2")
    msg2.id = "msg_2"
    msg3 = manager._build_message(parent_id, "user", "Message 3", meta={"checkpointHash": "hash2"})
    msg3.id = "msg_3"
    msg4 = manager._build_message(parent_id, "assistant", "Message 4")
    msg4.id = "msg_4"
    manager._save_messages(parent_id, [msg1, msg2, msg3, msg4])

    # 1. Fork up to msg_2
    child_id = manager.fork_session(parent_id, at_message_id_or_seq="msg_2")
    assert child_id is not None
    assert child_id != parent_id

    child_messages = manager.list_session_messages(child_id)
    assert len(child_messages) == 2
    assert child_messages[0].content == "Message 1"
    assert child_messages[1].content == "Message 2"
    assert child_messages[0].session_id == child_id
    assert child_messages[1].session_id == child_id

    # Check child entry in sessions index
    child_entry = manager._get_entry(child_id)
    assert child_entry is not None
    assert child_entry.get("forkOf") == parent_id
    assert child_entry.get("parentSessionId") == parent_id
    assert child_entry.get("forkPoint") == "msg_2"

    # 2. Fork entire session (at_message_id_or_seq=None)
    full_fork_id = manager.fork_session(parent_id)
    assert full_fork_id is not None
    full_messages = manager.list_session_messages(full_fork_id)
    assert len(full_messages) == 4


# ===========================================================================
# 3. Dual-Trigger Compaction & Shadow Events (Gap 6.3)
# ===========================================================================


def test_evaluate_compaction_trigger() -> None:
    context_limit = 100_000

    # Below 75% -> None
    assert evaluate_compaction_trigger(50_000, context_limit) is None
    assert evaluate_compaction_trigger(74_000, context_limit) is None

    # Between 75% and 95% -> pressure
    assert evaluate_compaction_trigger(75_000, context_limit) == "pressure"
    assert evaluate_compaction_trigger(90_000, context_limit) == "pressure"

    # At or above 95% -> overflow
    assert evaluate_compaction_trigger(95_000, context_limit) == "overflow"
    assert evaluate_compaction_trigger(100_000, context_limit) == "overflow"
    assert evaluate_compaction_trigger(110_000, context_limit) == "overflow"

    # Edge cases
    assert evaluate_compaction_trigger(0, context_limit) is None
    assert evaluate_compaction_trigger(1000, 0) is None


@pytest.mark.asyncio
async def test_compaction_shadow_events_and_triggers(tmp_path: pathlib.Path) -> None:
    # Setup dummy OpenAI client returning a summary response
    class FakeClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    class Choice:
                        class Message:
                            content = "Compacted conversation summary."

                        message = Message()

                    class Resp:
                        choices = [Choice()]
                        usage = {"total_tokens": 120}

                    return Resp()

    manager = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": FakeClient()},
        get_resolved_settings=lambda: {"model": "gpt-5.6-luna"},
    )

    session_id = f"ses_{uuid.uuid4().hex[:8]}"
    entry = {
        "id": session_id,
        "summary": "Compaction Test",
        "status": "ready",
        "activeTokens": 80_000,
    }
    manager._save_index({"version": 1, "entries": [entry], "originalPath": str(tmp_path)})

    # Append some messages
    m1 = manager._build_message(session_id, "user", "User question 1")
    m2 = manager._build_message(session_id, "assistant", "Assistant answer 1")
    m3 = manager._build_message(session_id, "user", "User question 2")
    m4 = manager._build_message(session_id, "assistant", "Assistant answer 2")
    manager._save_messages(session_id, [m1, m2, m3, m4])

    compaction = BasicCompaction(manager)
    result = await compaction.compact_now(session_id, trigger="pressure")
    assert result is not None
    assert result.summary == "Compacted conversation summary."
    assert result.shadowed_token_count == 120

    # Verify JSONL log contains shadow events
    path = manager._messages_path(session_id)
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    event_types = []
    for line in raw_lines:
        try:
            d = json.loads(line)
            if "type" in d:
                event_types.append(d["type"])
        except Exception:
            pass

    assert COMPACTION_START in event_types
    assert COMPACTION_SUMMARY in event_types
    assert COMPACTION_END in event_types


# ===========================================================================
# 4. Adaptive Context Budgeting (Gap 3.1)
# ===========================================================================


def test_adaptive_context_budgeting_calculations() -> None:
    # 1. DeepSeek 128k
    ds_budget = calculate_context_budget("deepseek-chat")
    assert ds_budget["context_limit"] == 128_000
    assert ds_budget["pressure_threshold"] == int(128_000 * 0.75)
    assert ds_budget["overflow_threshold"] == int(128_000 * 0.95)
    assert ds_budget["compaction_target_tokens"] == int(128_000 * 0.40)

    # 2. Claude 200k
    claude_budget = calculate_context_budget("claude-3-5-sonnet")
    assert claude_budget["context_limit"] == 200_000
    assert claude_budget["pressure_threshold"] == 150_000
    assert claude_budget["overflow_threshold"] == 190_000

    # 3. Gemini 1M+
    gemini_budget = calculate_context_budget("gemini-1.5-pro")
    assert gemini_budget["context_limit"] >= 1_000_000
    assert gemini_budget["pressure_threshold"] >= 750_000

    # 4. System and tool tokens reservation
    custom_budget = calculate_context_budget(
        "deepseek-chat",
        system_tokens=5000,
        tool_tokens=3000,
        safety_margin_tokens=2000,
    )
    assert custom_budget["reserved_system_tokens"] == 10000
    expected_available = 128_000 - custom_budget["max_output_tokens"] - 10000
    assert custom_budget["available_history_budget"] == expected_available

    # 5. Threshold lookup matches adaptive budget
    assert get_compact_prompt_token_threshold("deepseek-chat") == ds_budget["pressure_threshold"]
    assert (
        get_compact_prompt_token_threshold("claude-3-5-sonnet")
        == claude_budget["pressure_threshold"]
    )
