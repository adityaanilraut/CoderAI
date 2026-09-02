"""Tests for Phase 1 Capability Upgrades.

Verifies:
1. Synthetic abort tool results (TOOL_ABORTED_BEFORE_DISPATCH).
2. Dynamic tool concurrency and barrier scheduling.
3. Subagent report delivery modes ('next-step' vs 'quiet').
4. Cross-session references (@session:<id>) and snapshot extraction.
5. Package-level runtime invariant verification.
"""

from __future__ import annotations

import json
import pathlib
import pytest
from typing import Any

from coderai.core.common.invariants import (
    InvariantViolation,
    assert_session_invariants,
    verify_monotonic_sequence_numbers,
    verify_paired_tool_calls,
    verify_session_invariants,
    verify_turn_step_boundaries,
)
from coderai.core.common.session_reference import (
    extract_session_reference_ids,
    render_session_snapshot,
    resolve_session_references,
)
from coderai.cli.file_mention import expand_file_mentions
from coderai.core.session import SessionManager
from coderai.core.session_store import JsonlSessionStore
from coderai.core.tools.agents import handle_report_tool
from coderai.core.tools.schema import define_tool
from coderai.core.tools.types import (
    TOOL_ABORTED_BEFORE_DISPATCH,
    ToolExecutionContext,
    ToolResult,
)


# ==============================================================================
# 1. Dynamic Tool Concurrency & Barrier Scheduling
# ==============================================================================


def test_tool_definition_execution_mode():
    """Verify ToolDefinition execution mode evaluation."""
    tool_barrier = define_tool(
        name="barrier_tool",
        description="A barrier tool",
        execution_mode="barrier",
    )
    assert tool_barrier.check_execution_mode({}) == "barrier"

    tool_parallel = define_tool(
        name="parallel_tool",
        description="A parallel tool",
        execution_mode="parallel",
    )
    assert tool_parallel.check_execution_mode({}) == "parallel"

    tool_serial = define_tool(
        name="serial_tool",
        description="A serial tool",
        execution_mode="serial",
    )
    assert tool_serial.check_execution_mode({}) == "serial"

    # Dynamic callable execution mode
    tool_dynamic = define_tool(
        name="dynamic_tool",
        description="Dynamic tool",
        execution_mode=lambda args: "barrier" if args.get("flush") else "parallel",
    )
    assert tool_dynamic.check_execution_mode({"flush": True}) == "barrier"
    assert tool_dynamic.check_execution_mode({"flush": False}) == "parallel"


@pytest.mark.asyncio
async def test_session_barrier_tool_chunking_and_concludes_turn(tmp_path: pathlib.Path):
    """Verify barrier tools are partitioned into isolated chunks and concludes_turn stops subsequent calls."""
    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {},
        get_resolved_settings=lambda: {},
    )
    sid = await mgr.create_session("Test barrier prompt")
    mgr._update_entry(sid, lambda e: {**e, "status": "processing", "failReason": None})

    call_order: list[str] = []

    async def h_read(args: dict[str, Any], ctx: ToolExecutionContext) -> ToolResult:
        call_order.append(f"read_{args.get('path')}")
        return ToolResult(ok=True, name="read", output="file content")

    async def h_barrier(args: dict[str, Any], ctx: ToolExecutionContext) -> ToolResult:
        call_order.append(f"barrier_{args.get('op')}")
        return ToolResult(ok=True, name="barrier", output="barrier done")

    async def h_conclude(args: dict[str, Any], ctx: ToolExecutionContext) -> ToolResult:
        call_order.append("conclude")
        return ToolResult(ok=True, name="conclude", output="finished", concludes_turn=True)

    mgr.tool_executor.registry.register(
        define_tool(name="read_test", handler=h_read, execution_mode="parallel")
    )
    mgr.tool_executor.registry.register(
        define_tool(name="barrier_test", handler=h_barrier, execution_mode="barrier")
    )
    mgr.tool_executor.registry.register(
        define_tool(name="conclude_test", handler=h_conclude, execution_mode="serial")
    )

    tool_calls = [
        {
            "id": "tc1",
            "type": "function",
            "function": {"name": "read_test", "arguments": json.dumps({"path": "a.py"})},
        },
        {
            "id": "tc2",
            "type": "function",
            "function": {"name": "read_test", "arguments": json.dumps({"path": "b.py"})},
        },
        {
            "id": "tc3",
            "type": "function",
            "function": {"name": "barrier_test", "arguments": json.dumps({"op": "sync"})},
        },
        {"id": "tc4", "type": "function", "function": {"name": "conclude_test", "arguments": "{}"}},
        {
            "id": "tc5",
            "type": "function",
            "function": {"name": "read_test", "arguments": json.dumps({"path": "c.py"})},
        },
    ]

    await mgr._append_tool_messages(sid, tool_calls)

    # Verify tc1, tc2, tc3, tc4 ran; tc5 was skipped due to concludes_turn
    assert "read_a.py" in call_order
    assert "read_b.py" in call_order
    assert "barrier_sync" in call_order
    assert "conclude" in call_order
    assert "read_c.py" not in call_order

    # Verify synthetic abort was recorded for tc5
    messages = mgr.list_session_messages(sid)
    tc5_msg = next((m for m in messages if m.tool_call_id == "tc5"), None)
    assert tc5_msg is not None
    assert TOOL_ABORTED_BEFORE_DISPATCH in tc5_msg.content


# ==============================================================================
# 2. Synthetic Abort Tool Results on Interrupt
# ==============================================================================


@pytest.mark.asyncio
async def test_synthetic_abort_results_on_session_interrupt(tmp_path: pathlib.Path):
    """Verify unexecuted tool calls receive TOOL_ABORTED_BEFORE_DISPATCH on cancellation."""
    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {},
        get_resolved_settings=lambda: {},
    )
    sid = await mgr.create_session("Test interrupt prompt")
    mgr._update_entry(sid, lambda e: {**e, "status": "processing", "failReason": None})

    async def h_slow(args: dict[str, Any], ctx: ToolExecutionContext) -> ToolResult:
        # Trigger interruption while executing first tool
        mgr.interrupt_session(sid)
        return ToolResult(ok=True, name="slow_tool", output="first done")

    async def h_never(args: dict[str, Any], ctx: ToolExecutionContext) -> ToolResult:
        return ToolResult(ok=True, name="never_tool", output="should not run")

    mgr.tool_executor.registry.register(
        define_tool(name="slow_tool", handler=h_slow, execution_mode="serial")
    )
    mgr.tool_executor.registry.register(
        define_tool(name="never_tool", handler=h_never, execution_mode="serial")
    )

    tool_calls = [
        {"id": "call_1", "type": "function", "function": {"name": "slow_tool", "arguments": "{}"}},
        {"id": "call_2", "type": "function", "function": {"name": "never_tool", "arguments": "{}"}},
        {"id": "call_3", "type": "function", "function": {"name": "never_tool", "arguments": "{}"}},
    ]

    await mgr._append_tool_messages(sid, tool_calls)

    messages = mgr.list_session_messages(sid)
    # Every tool call must have a response message
    tool_msgs = [m for m in messages if m.role == "tool"]
    assert len(tool_msgs) == 3

    msg1 = next(m for m in tool_msgs if m.tool_call_id == "call_1")
    assert "first done" in msg1.content

    msg2 = next(m for m in tool_msgs if m.tool_call_id == "call_2")
    assert TOOL_ABORTED_BEFORE_DISPATCH in msg2.content

    msg3 = next(m for m in tool_msgs if m.tool_call_id == "call_3")
    assert TOOL_ABORTED_BEFORE_DISPATCH in msg3.content

    # Validate session invariants hold: zero dangling tool calls
    assert_session_invariants(messages)


# ==============================================================================
# 3. Subagent Report Delivery Modes
# ==============================================================================


@pytest.mark.asyncio
async def test_subagent_report_tool_delivery_modes(tmp_path: pathlib.Path):
    """Verify handle_report_tool supports next-step and quiet delivery."""
    ctx = ToolExecutionContext(session_id="sub_test_123", project_root=str(tmp_path))

    # Test default next-step delivery
    res1 = await handle_report_tool({"summary": "Exploration finished"}, ctx)
    assert res1.ok
    assert res1.metadata["delivery"] == "next-step"
    assert res1.metadata["summary"] == "Exploration finished"

    # Test quiet delivery
    res2 = await handle_report_tool(
        {"summary": "Background metric cached", "delivery": "quiet"}, ctx
    )
    assert res2.ok
    assert res2.metadata["delivery"] == "quiet"


# ==============================================================================
# 4. Cross-Session Reference Injection (@session:<id>)
# ==============================================================================


def test_extract_session_reference_ids():
    """Verify extracting @session:id and session:id tokens."""
    text1 = "Please review @session:ses_abc12345 and see how we fixed it."
    assert extract_session_reference_ids(text1) == ["ses_abc12345"]

    text2 = "Compare @session:ses_11111111 with session:ses_22222222 and session:ses_33333333"
    ids = extract_session_reference_ids(text2)
    assert ids == ["ses_11111111", "ses_22222222", "ses_33333333"]

    assert extract_session_reference_ids("No mentions here") == []


def test_render_and_resolve_session_references(tmp_path: pathlib.Path):
    """Verify loading and formatting historical session snapshots."""
    store = JsonlSessionStore(str(tmp_path))
    sid = "ses_historical_01"

    # Seed a past session
    events = [
        {
            "seq": 1,
            "type": "session/created",
            "sessionId": sid,
            "timestamp": "2026-08-27T00:00:00Z",
        },
        {"seq": 2, "role": "user", "content": "Implement user authentication with JWT tokens"},
        {
            "seq": 3,
            "role": "assistant",
            "tool_calls": [{"id": "t1", "function": {"name": "write"}}],
            "content": "Creating auth handler",
        },
        {"seq": 4, "role": "tool", "tool_call_id": "t1", "content": "File written"},
        {
            "seq": 5,
            "role": "assistant",
            "content": "Successfully implemented JWT auth in auth.py with refresh rotation.",
        },
    ]
    msg_path = store.messages_path(sid)
    msg_path.parent.mkdir(parents=True, exist_ok=True)
    msg_path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

    # Render snapshot
    snapshot = render_session_snapshot(sid, str(tmp_path))
    assert snapshot is not None
    assert sid in snapshot
    assert "Implement user authentication with JWT tokens" in snapshot
    assert "Successfully implemented JWT auth" in snapshot
    assert "write" in snapshot

    # Resolve references in prompt
    prompt = "Please build on @session:ses_historical_01 to add OAuth2 support."
    clean, refs, ctx_block = resolve_session_references(str(tmp_path), prompt)
    assert len(refs) == 1
    assert refs[0]["sessionId"] == sid
    assert refs[0]["resolved"] is True
    assert "Referenced Historical Session" in ctx_block
    assert "JWT" in ctx_block

    # Test integration with expand_file_mentions
    expanded_prompt, attached = expand_file_mentions(prompt, str(tmp_path))
    assert f"session:{sid}" in attached
    assert "Referenced Prior Sessions" in expanded_prompt


# ==============================================================================
# 5. Runtime Invariant Verification
# ==============================================================================


def test_runtime_invariants_verification():
    """Verify runtime invariant checker catches sequence gaps, dangling calls, and broken boundaries."""
    # 1. Valid session
    valid_events = [
        {"seq": 1, "type": "turn/start"},
        {"seq": 2, "type": "step/start"},
        {"seq": 3, "role": "user", "content": "hello"},
        {
            "seq": 4,
            "role": "assistant",
            "tool_calls": [{"id": "t1", "function": {"name": "read"}}],
            "content": "",
        },
        {"seq": 5, "role": "tool", "tool_call_id": "t1", "content": "ok"},
        {"seq": 6, "role": "assistant", "content": "Hi there!"},
        {"seq": 7, "type": "step/end"},
        {"seq": 8, "type": "turn/end"},
    ]
    assert verify_session_invariants(valid_events) == []
    assert_session_invariants(valid_events)

    # 2. Non-monotonic sequence
    bad_seq = [
        {"seq": 1, "type": "turn/start"},
        {"seq": 3, "type": "step/start"},
        {"seq": 2, "role": "user", "content": "hello"},
    ]
    v_seq = verify_monotonic_sequence_numbers(bad_seq)
    assert len(v_seq) > 0
    assert "non-monotonic" in v_seq[0]

    # 3. Dangling tool call without result
    dangling_calls = [
        {
            "role": "assistant",
            "tool_calls": [{"id": "call_orphan", "function": {"name": "read"}}],
            "content": "",
        },
        {"role": "user", "content": "Next turn prompt"},
    ]
    v_calls = verify_paired_tool_calls(dangling_calls)
    assert len(v_calls) > 0
    assert "call_orphan" in v_calls[0]

    # 4. Turn/step boundary mismatch
    bad_boundaries = [
        {"type": "turn/start"},
        {"type": "turn/start"},  # Nested turn
    ]
    v_bound = verify_turn_step_boundaries(bad_boundaries)
    assert len(v_bound) > 0
    assert "Nested turn/start" in v_bound[0]

    # 5. Assert raises InvariantViolation
    with pytest.raises(InvariantViolation) as exc_info:
        assert_session_invariants(bad_seq)
    assert "non-monotonic" in str(exc_info.value)
