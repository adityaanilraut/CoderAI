"""Explicit session YOLO remains idempotent and preserves force-confirm gates.

Regression coverage for:
* ``set_auto_approve`` is idempotent (cannot accidentally flip YOLO off)
* confirmation callback re-checks ``auto_approve`` after acquiring the lock
* MCP mutation gate still forces confirmation under YOLO (``force_confirm``)
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from coderAI.core.execution_context import RunContext
from coderAI.core.tool_executor import ToolExecutor
from coderAI.tui.commands import _cmd_set_auto_approve, _cmd_tool_approval_resp


def _executor_agent(**overrides):
    values = {
        "auto_approve": False,
        "approval_port": None,
        "confirmation_override": None,
        "tracker_info": None,
        "tracker_update": MagicMock(),
        "_sync_tracker": MagicMock(),
        "config": SimpleNamespace(approval_timeout_seconds=300),
        "tools": SimpleNamespace(get=MagicMock(return_value=None)),
        "_tool_approval_allowlist": set(),
        "session": None,
        "context_controller": SimpleNamespace(summarize_tool_result=lambda result: result),
        "active_plan_id": None,
        "active_plan_revision": None,
        "_plan_execution_ready": False,
        "plan_mode": False,
        "_allowed_native_tool_names": None,
        "_capability_domain": None,
        "run_context": RunContext(),
        "_workspace_trusted": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_explicit_yolo_can_be_enabled_before_approval_resp() -> None:
    """The explicit unsafe-mode API remains usable outside the approval modal."""
    agent = SimpleNamespace(
        auto_approve=False,
        _configure_delegate_tool_context=MagicMock(),
        _refresh_session_system_prompt=MagicMock(),
    )
    server = SimpleNamespace(agent=agent, emit=MagicMock(), _approval_waiters={})
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    server._approval_waiters["t1"] = fut

    # Explicitly enable YOLO first, then resolve the pending approval.
    await _cmd_set_auto_approve(server, {"enabled": True})
    assert agent.auto_approve is True

    await _cmd_tool_approval_resp(server, {"toolId": "t1", "approve": True})
    assert fut.result() is True


@pytest.mark.asyncio
async def test_confirmation_skips_prompt_when_yolo_enabled_while_queued() -> None:
    """A tool queued behind _confirm_lock must honour Always without re-prompting."""
    prompted = False

    class FakePort:
        async def request(self, tool, args, preview=None):
            from coderAI.core.ports import Approval

            nonlocal prompted
            prompted = True
            return Approval(allowed=True)

    agent = _executor_agent(
        auto_approve=False,
        approval_port=FakePort(),
        tracker_info=None,
        _sync_tracker=MagicMock(),
        config=SimpleNamespace(approval_timeout_seconds=300),
        confirmation_override=None,
    )
    executor = ToolExecutor(agent)

    await executor._confirm_lock.acquire()
    try:
        task = asyncio.create_task(
            executor._confirmation_callback("web_search", {"query": "a"}, tool_id="t1")
        )
        await asyncio.sleep(0)  # let the task block on the lock
        agent.auto_approve = True  # Always pressed while queued
    finally:
        executor._confirm_lock.release()

    assert await task is True
    assert prompted is False


@pytest.mark.asyncio
async def test_force_confirm_still_prompts_under_yolo() -> None:
    """MCP mutation gate passes force_confirm=True and must still prompt."""
    prompted = False

    class FakePort:
        async def request(self, tool, args, preview=None):
            from coderAI.core.ports import Approval

            nonlocal prompted
            prompted = True
            return Approval(allowed=True)

    agent = _executor_agent(
        auto_approve=True,  # YOLO on
        approval_port=FakePort(),
        tracker_info=None,
        _sync_tracker=MagicMock(),
        config=SimpleNamespace(approval_timeout_seconds=300),
        confirmation_override=None,
    )
    executor = ToolExecutor(agent)

    # Without force: YOLO short-circuits.
    assert (
        await executor._confirmation_callback(
            "write_file", {"path": "x", "content": "y"}, tool_id="t1"
        )
        is True
    )
    assert prompted is False

    # With force (MCP mutation gate): still prompts.
    assert (
        await executor._confirmation_callback(
            "write_file",
            {"path": "x", "content": "y"},
            tool_id="t2",
            force_confirm=True,
        )
        is True
    )
    assert prompted is True


@pytest.mark.asyncio
async def test_execute_skips_confirm_after_yolo_mid_batch() -> None:
    """Once auto_approve flips on, a subsequent gated tool skips confirmation."""
    registry = SimpleNamespace(
        get=MagicMock(
            return_value=SimpleNamespace(
                requires_confirmation=True,
                is_read_only=False,
                is_egress=False,
                safe=False,
                high_risk_no_blanket=False,
                retryable=False,
            )
        ),
        execute=AsyncMock(return_value={"success": True}),
    )
    agent = _executor_agent(
        auto_approve=False,
        approval_port=None,
        tools=registry,
        tracker_info=None,
        _sync_tracker=MagicMock(),
        _tool_approval_allowlist=set(),
        confirmation_override=None,
        session=None,
        context_controller=SimpleNamespace(summarize_tool_result=lambda r: r),
        config=SimpleNamespace(),
    )
    hooks_manager = SimpleNamespace(
        run_hooks=AsyncMock(return_value=[]),
        run_permission_hooks=AsyncMock(return_value=None),
    )
    executor = ToolExecutor(agent)
    confirm = AsyncMock(return_value=True)
    executor._confirmation_callback = confirm

    # First call needs confirmation.
    r1 = await executor.execute_single_tool(
        {
            "tool_id": "t1",
            "tool_name": "write_file",
            "arguments": {"path": "a", "content": "1"},
            "parse_error": None,
        },
        hooks_data=None,
        hooks_manager=hooks_manager,
    )
    assert r1["success"] is True
    assert confirm.await_count == 1

    # Always enabled YOLO.
    agent.auto_approve = True
    confirm.reset_mock()

    r2 = await executor.execute_single_tool(
        {
            "tool_id": "t2",
            "tool_name": "write_file",
            "arguments": {"path": "b", "content": "2"},
            "parse_error": None,
        },
        hooks_data=None,
        hooks_manager=hooks_manager,
    )
    assert r2["success"] is True
    confirm.assert_not_awaited()
