"""Tests for one-shot length finish_reason recovery in the execution loop."""

from unittest.mock import AsyncMock

import pytest

from coderAI.core.agent_loop import ExecutionLoop
from coderAI.core.tool_executor import BatchStatus, ToolBatchOutcome

TOOL_CALL_RESPONSE = {
    "content": None,
    "tool_calls": [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"x"}'},
        }
    ],
}


@pytest.mark.asyncio
async def test_one_auto_retry_on_length_when_tools_were_used(mock_agent):
    loop = ExecutionLoop(mock_agent)
    llm_responses = [
        TOOL_CALL_RESPONSE,
        {"finish_reason": "length", "content": "partial"},
        {"finish_reason": "stop", "content": "final answer"},
    ]
    loop._call_llm_with_retry = AsyncMock(side_effect=llm_responses)
    loop.tool_executor.orchestrate_tool_calls = AsyncMock(
        return_value=ToolBatchOutcome(BatchStatus.OK)
    )

    result = await loop.run("do work")

    assert result["content"] == "final answer"
    assert loop._call_llm_with_retry.await_count == 3
    assert loop._length_retry_used is True


@pytest.mark.asyncio
async def test_second_length_is_terminal_without_third_retry(mock_agent):
    loop = ExecutionLoop(mock_agent)
    llm_responses = [
        TOOL_CALL_RESPONSE,
        {"finish_reason": "length", "content": "partial one"},
        {"finish_reason": "length", "content": "partial two"},
    ]
    loop._call_llm_with_retry = AsyncMock(side_effect=llm_responses)
    loop.tool_executor.orchestrate_tool_calls = AsyncMock(
        return_value=ToolBatchOutcome(BatchStatus.OK)
    )

    result = await loop.run("do work")

    assert loop._call_llm_with_retry.await_count == 3
    assert "max_tokens" in result["content"].lower()
