"""Tests for per-iteration backoff after recoverable errors."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coderAI.core.agent_loop import ExecutionLoop
from coderAI.core.agent_tracker import AgentInfo, AgentStatus
from coderAI.system.error_policy import RETRY_MAX_DELAY, compute_iteration_backoff
from coderAI.system.history import Session


class TestComputeIterationBackoff:
    def test_zero_errors_no_delay(self):
        assert compute_iteration_backoff(0) == 0.0

    def test_backoff_increases_and_caps(self):
        assert compute_iteration_backoff(1) == 0.5
        assert compute_iteration_backoff(2) == 1.0
        cap = RETRY_MAX_DELAY / 2
        assert compute_iteration_backoff(10) == cap


@pytest.fixture
def loop_agent():
    session = Session(session_id="session_1234567890_backoff01")
    context_controller = SimpleNamespace(
        inject_context=lambda messages, _cm, query=None: messages,
        manage_context_window=AsyncMock(side_effect=lambda messages: messages),
    )
    agent = SimpleNamespace(
        session=session,
        config=SimpleNamespace(
            max_iterations=5,
            max_iterations_hard_cap=200,
            budget_limit=0,
            continue_loop_on_deny=True,
        ),
        cost_tracker=MagicMock(get_total_cost=MagicMock(return_value=0)),
        context_controller=context_controller,
        context_manager=SimpleNamespace(),
        hooks_manager=SimpleNamespace(
            load_hooks=MagicMock(return_value=None),
            run_hooks=AsyncMock(return_value=[]),
        ),
        _assistant_reply_parts=[],
        _register_tracker=MagicMock(),
        _sync_tracker=MagicMock(),
        _finish_tracker=MagicMock(),
        save_session=MagicMock(),
        tracker_info=None,
        read_cache=None,
        provider=MagicMock(get_model_info=MagicMock(return_value={})),
        tools=MagicMock(get_schemas=MagicMock(return_value=[])),
    )
    return agent


@pytest.mark.asyncio
async def test_execution_loop_applies_backoff_after_errors(loop_agent):
    loop = ExecutionLoop(loop_agent)
    call_count = 0

    async def flaky_llm(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("transient boom")
        return {"content": "ok", "tool_calls": None, "finish_reason": "stop"}

    loop._call_llm_with_retry = flaky_llm
    sleep_mock = AsyncMock()
    with patch("coderAI.core.agent_loop.asyncio.sleep", sleep_mock):
        result = await loop.run("hello")

    assert result["content"] == "ok"
    sleep_mock.assert_awaited_once()
    assert sleep_mock.await_args.args[0] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_cancellation_during_backoff_exits(loop_agent):
    info = AgentInfo(
        agent_id="agent_backoff_cancel",
        name="main",
        status=AgentStatus.THINKING,
    )
    loop_agent.tracker_info = info

    loop = ExecutionLoop(loop_agent)
    call_count = 0

    async def flaky_llm(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("transient boom")
        return {"content": "should not reach", "tool_calls": None}

    loop._call_llm_with_retry = flaky_llm

    real_wait_for = asyncio.wait_for

    async def wait_for_cancel_during_backoff(coro, timeout=None):
        if (
            loop_agent.tracker_info is not None
            and not loop_agent.tracker_info.is_cancelled
            and timeout is not None
            and timeout > 0
        ):
            loop_agent.tracker_info.request_cancel()
        return await real_wait_for(coro, timeout=timeout)

    with patch(
        "coderAI.core.agent_loop.asyncio.wait_for", side_effect=wait_for_cancel_during_backoff
    ):
        result = await loop.run("hello")

    assert "stopped by user" in result["content"].lower()
    assert call_count == 1
