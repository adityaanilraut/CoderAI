"""Tests for per-iteration backoff after recoverable errors."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from coderAI.core.agent_loop import ExecutionLoop
from coderAI.core.agent_tracker import AgentInfo, AgentStatus
from coderAI.system.error_policy import RETRY_MAX_DELAY, compute_iteration_backoff


class TestComputeIterationBackoff:
    def test_zero_errors_no_delay(self):
        assert compute_iteration_backoff(0) == 0.0

    def test_backoff_increases_and_caps(self):
        assert compute_iteration_backoff(1) == 0.5
        assert compute_iteration_backoff(2) == 1.0
        cap = RETRY_MAX_DELAY / 2
        assert compute_iteration_backoff(10) == cap


@pytest.mark.asyncio
async def test_execution_loop_applies_backoff_after_errors(mock_agent):
    mock_agent.config.max_iterations = 5
    loop = ExecutionLoop(mock_agent)
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
async def test_cancellation_during_backoff_exits(mock_agent):
    info = AgentInfo(
        agent_id="agent_backoff_cancel",
        name="main",
        status=AgentStatus.THINKING,
    )
    mock_agent.tracker_info = info

    loop = ExecutionLoop(mock_agent)
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
            mock_agent.tracker_info is not None
            and not mock_agent.tracker_info.is_cancelled
            and timeout is not None
            and timeout > 0
        ):
            mock_agent.tracker_info.request_cancel()
        return await real_wait_for(coro, timeout=timeout)

    with patch(
        "coderAI.core.agent_loop.asyncio.wait_for", side_effect=wait_for_cancel_during_backoff
    ):
        result = await loop.run("hello")

    assert "stopped by user" in result["content"].lower()
    assert call_count == 1


@pytest.mark.asyncio
async def test_cancellation_interrupts_stalled_provider_request(mock_agent):
    info = AgentInfo(
        agent_id="agent_request_cancel",
        name="main",
        status=AgentStatus.THINKING,
    )
    mock_agent.tracker_info = info
    mock_agent.streaming = False
    mock_agent.context_controller.request_tool_schemas = None
    mock_agent.context_controller._SUMMARY_MARKER_KEY = "_context_summary"
    mock_agent.context_controller.strip_internal_markers = lambda messages: messages
    mock_agent.provider.clean_messages = lambda messages: messages
    started = asyncio.Event()

    async def stalled_request(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    mock_agent.provider.chat = stalled_request
    loop = ExecutionLoop(mock_agent)
    request = asyncio.create_task(loop._call_llm_with_retry([], []))
    await started.wait()
    info.request_cancel()

    result = await asyncio.wait_for(request, timeout=1)

    assert result["finish_reason"] == "cancelled"
