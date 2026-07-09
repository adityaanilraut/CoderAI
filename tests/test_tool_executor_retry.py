"""Transient-failure retry semantics in ToolExecutor (mirrors
test_tool_executor_timeout.py's setup style)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from coderAI.core.services import services_scope
from coderAI.core.tool_executor import ToolExecutor
from coderAI.system.config import Config
from coderAI.tools.base import Tool, ToolRegistry

# Retry sleeps are zeroed via tool_retry_base_delay=0.0 (jitter scales off the
# base, so the whole delay collapses to 0).
FAST_RETRY_CONFIG = dict(tool_retry_base_delay=0.0)


def _executor_for(tool, *, auto_approve=True, tracker_info=None):
    agent = MagicMock()
    agent.auto_approve = auto_approve
    agent.tracker_info = tracker_info
    agent._tool_approval_allowlist = None
    registry = ToolRegistry()
    registry.register(tool)
    agent.tools = registry
    return ToolExecutor(agent), agent


def _hooks():
    hm = AsyncMock()
    hm.run_hooks.return_value = None
    return hm


def _pc(name):
    return {"tool_id": "t1", "tool_name": name, "arguments": {}}


class FlakyRaiseTool(Tool):
    """Raises a transient error on the first call, then succeeds."""

    name = "flaky_raise"
    is_read_only = True
    retryable = True

    def __init__(self, fail_times=1, message="connection reset by peer"):
        self.calls = 0
        self.fail_times = fail_times
        self.message = message

    async def execute(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError(self.message)
        return {"success": True, "output": "ok"}


class FlakyDictTool(Tool):
    """Returns a failure dict on the first call, then succeeds."""

    name = "flaky_dict"
    is_read_only = True
    retryable = True

    def __init__(self, error="503 Service Unavailable"):
        self.calls = 0
        self.error = error

    async def execute(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return {"success": False, "error": self.error}
        return {"success": True, "output": "ok"}


async def test_flaky_retryable_tool_succeeds_on_second_attempt():
    tool = FlakyRaiseTool()
    executor, _ = _executor_for(tool)
    with services_scope(config=Config(**FAST_RETRY_CONFIG)):
        result = await executor.execute_single_tool(_pc(tool.name), None, _hooks())
    assert result["success"] is True
    assert tool.calls == 2


async def test_non_retryable_tool_is_not_retried():
    tool = FlakyRaiseTool()
    tool.retryable = False
    executor, _ = _executor_for(tool)
    with services_scope(config=Config(**FAST_RETRY_CONFIG)):
        result = await executor.execute_single_tool(_pc(tool.name), None, _hooks())
    assert result["success"] is False
    assert "connection reset" in result["error"]
    assert tool.calls == 1


async def test_confirmation_gated_tool_approved_once_and_not_retried():
    """An approval covers exactly the attempt the user saw — never a retry."""
    tool = FlakyRaiseTool(fail_times=99)  # would keep failing transiently
    tool.requires_confirmation = True
    executor, agent = _executor_for(tool, auto_approve=False)
    approvals = AsyncMock(return_value=True)
    agent.confirmation_override = approvals
    with services_scope(config=Config(**FAST_RETRY_CONFIG)):
        result = await executor.execute_single_tool(_pc(tool.name), None, _hooks())
    assert result["success"] is False
    assert approvals.await_count == 1
    assert tool.calls == 1


async def test_transient_error_dict_is_retried():
    tool = FlakyDictTool(error="503 Service Unavailable")
    executor, _ = _executor_for(tool)
    with services_scope(config=Config(**FAST_RETRY_CONFIG)):
        result = await executor.execute_single_tool(_pc(tool.name), None, _hooks())
    assert result["success"] is True
    assert tool.calls == 2


async def test_permanent_error_dict_is_not_retried():
    tool = FlakyDictTool(error="file not found")
    executor, _ = _executor_for(tool)
    with services_scope(config=Config(**FAST_RETRY_CONFIG)):
        result = await executor.execute_single_tool(_pc(tool.name), None, _hooks())
    assert result["success"] is False
    assert tool.calls == 1


async def test_executor_timeout_is_never_retried():
    class SlowRetryableTool(Tool):
        name = "slow_retryable"
        is_read_only = True
        retryable = True
        timeout = 0.05

        def __init__(self):
            self.calls = 0

        async def execute(self, **kwargs):
            self.calls += 1
            await asyncio.sleep(0.3)
            return {"success": True}

    tool = SlowRetryableTool()
    executor, _ = _executor_for(tool)
    with services_scope(config=Config(**FAST_RETRY_CONFIG)):
        result = await executor.execute_single_tool(_pc(tool.name), None, _hooks())
    assert result["success"] is False
    assert result["error_code"] == "timeout"
    assert tool.calls == 1


async def test_zero_max_attempts_disables_retry():
    tool = FlakyRaiseTool()
    executor, _ = _executor_for(tool)
    with services_scope(config=Config(tool_retry_max_attempts=0, **FAST_RETRY_CONFIG)):
        result = await executor.execute_single_tool(_pc(tool.name), None, _hooks())
    assert result["success"] is False
    assert tool.calls == 1


async def test_cancel_event_aborts_retries():
    cancel_event = asyncio.Event()
    cancel_event.set()
    tracker_info = SimpleNamespace(agent_id="main", _cancel_event=cancel_event)
    tool = FlakyRaiseTool(fail_times=99)
    executor, _ = _executor_for(tool, tracker_info=tracker_info)
    with services_scope(config=Config(**FAST_RETRY_CONFIG)):
        result = await executor.execute_single_tool(_pc(tool.name), None, _hooks())
    assert result["success"] is False
    assert tool.calls == 1
