"""Tests for Repeat Tool Reminder Guard and Timeout Policy."""

from __future__ import annotations

import asyncio
import pytest
from coderai.core.guards.repeat_reminder import RepeatToolReminderGuard
from coderai.core.guards.timeout import execute_with_timeout_policy, TOOL_TIMEOUT_ERROR_CODE
from coderai.core.tools.types import ToolResult


def test_repeat_tool_reminder_thresholds():
    guard = RepeatToolReminderGuard(thresholds=[3, 5])

    # Call 1 & 2 -> No reminder
    assert guard.record_call("sess_a", "read_file", {"path": "a.txt"}) is None
    assert guard.record_call("sess_a", "read_file", {"path": "a.txt"}) is None

    # Call 3 -> Fires threshold 3 reminder
    r3 = guard.record_call("sess_a", "read_file", {"path": "a.txt"})
    assert r3 is not None
    assert "3 consecutive times" in r3

    # Call 4 -> None
    assert guard.record_call("sess_a", "read_file", {"path": "a.txt"}) is None

    # Call 5 -> Fires threshold 5 reminder
    r5 = guard.record_call("sess_a", "read_file", {"path": "a.txt"})
    assert r5 is not None
    assert "5 consecutive times" in r5

    # Changing args resets count
    assert guard.record_call("sess_a", "read_file", {"path": "b.txt"}) is None


@pytest.mark.asyncio
async def test_execute_with_timeout_policy():
    async def fast_task():
        return ToolResult(ok=True, name="bash", output="hello")

    async def slow_task():
        await asyncio.sleep(1.0)
        return ToolResult(ok=True, name="bash", output="late")

    # Fast task finishes within deadline
    r_fast = await execute_with_timeout_policy(fast_task, "bash", timeout_seconds=1.0)
    assert r_fast.ok is True
    assert r_fast.output == "hello"

    # Slow task times out
    r_slow = await execute_with_timeout_policy(slow_task, "bash", timeout_seconds=0.05)
    assert r_slow.ok is False
    assert TOOL_TIMEOUT_ERROR_CODE in r_slow.error
