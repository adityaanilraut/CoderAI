"""Hermetic ExecutionLoop integration tests — mocked provider driving full loop."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coderAI.core.agent_loop import ExecutionLoop
from coderAI.core.tool_executor import BatchStatus


@pytest.mark.asyncio
async def test_hermetic_tool_call_then_success(mock_agent):
    """One tool call → executor success → stop, verifying repair invariant."""
    mock_agent.config.max_iterations = 5
    mock_agent.provider.supports_tools.return_value = True
    # Make sure tool routing returns a simple schema
    mock_agent.tools.get_schemas.return_value = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "x",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    mock_agent.tools.get_all.return_value = []

    # First LLM turn returns tool_calls, second returns final stop
    responses = [
        {
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'},
                }
            ],
            "finish_reason": "tool_calls",
        },
        {"content": "done", "tool_calls": None, "finish_reason": "stop"},
    ]
    mock_agent.provider.chat = AsyncMock(
        side_effect=[
            {"choices": [{"message": r, "finish_reason": r.get("finish_reason")}], "usage": {}}
            for r in responses
        ]
    )
    # But ExecutionLoop uses _call_llm_with_retry -> provider.chat via _extract_response_data path when not streaming.
    # Simpler: mock _call_llm_with_retry directly
    loop = ExecutionLoop(mock_agent)
    call_idx = 0

    async def fake_call(messages, tools):
        nonlocal call_idx
        r = responses[call_idx]
        call_idx += 1
        return r

    loop._call_llm_with_retry = fake_call  # type: ignore[method-assign]

    # Mock tool executor to return OK
    from coderAI.core.tool_executor import ToolBatchOutcome

    loop.tool_executor.orchestrate_tool_calls = AsyncMock(
        return_value=ToolBatchOutcome(BatchStatus.OK)
    )

    result = await loop.run("hello tool")

    assert result["success"] is True
    assert "done" in result["content"]
    # Repair invariant: every assistant tool_call has matching tool message count
    # (executor already persisted tool messages, but we check via call count)
    assert loop.tool_executor.orchestrate_tool_calls.await_count == 1


@pytest.mark.asyncio
async def test_hermetic_unknown_finish_reason_terminal(mock_agent):
    """Unknown finish_reason should not silently enter tool phase."""
    mock_agent.config.max_iterations = 5
    mock_agent.provider.supports_tools.return_value = True
    mock_agent.tools.get_schemas.return_value = []
    mock_agent.tools.get_all.return_value = []

    loop = ExecutionLoop(mock_agent)
    loop._call_llm_with_retry = AsyncMock(
        return_value={"content": "filtered", "tool_calls": None, "finish_reason": "content_filter"}
    )  # type: ignore[method-assign]

    result = await loop.run("hello")

    assert result["stop_reason"] == "content_filter"
    assert result["success"] is False


@pytest.mark.asyncio
async def test_hermetic_refusal_does_not_loop(mock_agent):
    mock_agent.config.max_iterations = 5
    mock_agent.provider.supports_tools.return_value = True
    mock_agent.tools.get_schemas.return_value = []
    mock_agent.tools.get_all.return_value = []

    loop = ExecutionLoop(mock_agent)
    loop._call_llm_with_retry = AsyncMock(
        return_value={"content": "I refuse", "tool_calls": None, "finish_reason": "refusal"}
    )  # type: ignore[method-assign]

    result = await loop.run("do bad thing")

    assert result["stop_reason"] == "refusal"
    assert "refuse" in result["content"].lower()


@pytest.mark.asyncio
async def test_hermetic_programming_error_fails_fast(mock_agent):
    """TypeError from provider should hit fatal path, not recoverable loop."""
    mock_agent.config.max_iterations = 5
    mock_agent.provider.supports_tools.return_value = True
    mock_agent.tools.get_schemas.return_value = []
    mock_agent.tools.get_all.return_value = []

    loop = ExecutionLoop(mock_agent)

    async def boom(*_a, **_kw):
        raise TypeError("programming bug")

    loop._call_llm_with_retry = boom  # type: ignore[method-assign]

    result = await loop.run("hello")

    assert result["stop_reason"] == "error"
    assert result["success"] is False
