"""Terminal exits must not leave unpaired assistant tool_calls in the session."""

from unittest.mock import AsyncMock

import pytest

from coderAI.core.agent_loop import ExecutionLoop
from coderAI.system.error_policy import MAX_CONSECUTIVE_PAUSES


@pytest.mark.asyncio
async def test_refusal_with_tool_calls_repairs_transcript(mock_agent):
    loop = ExecutionLoop(mock_agent)
    loop._call_llm_with_retry = AsyncMock(
        return_value={
            "content": "I cannot help with that.",
            "tool_calls": [
                {
                    "id": "call_refused",
                    "type": "function",
                    "function": {"name": "run_command", "arguments": "{}"},
                }
            ],
            "finish_reason": "refusal",
        }
    )

    result = await loop.run("do something bad")

    assert result["stop_reason"] == "refusal"
    msgs = mock_agent.session.messages
    assistant = next(m for m in msgs if m.role == "assistant")
    assert assistant.tool_calls
    tool_ids = {m.tool_call_id for m in msgs if m.role == "tool"}
    assert "call_refused" in tool_ids


@pytest.mark.asyncio
async def test_in_batch_doom_repairs_unpaired_tool_calls(mock_agent):
    loop = ExecutionLoop(mock_agent)
    dup_calls = [
        {
            "id": f"c{i}",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"x"}'},
        }
        for i in range(3)
    ]
    loop._call_llm_with_retry = AsyncMock(
        return_value={"content": None, "tool_calls": dup_calls, "finish_reason": "tool_calls"}
    )
    loop.tool_executor.orchestrate_tool_calls = AsyncMock(
        side_effect=AssertionError("executor must not run after in-batch doom")
    )

    result = await loop.run("go")

    assert result["stop_reason"] == "doom_loop"
    tool_ids = {m.tool_call_id for m in mock_agent.session.messages if m.role == "tool"}
    assert tool_ids == {"c0", "c1", "c2"}


@pytest.mark.asyncio
async def test_pause_storm_uses_dedicated_stop_reason(mock_agent):
    loop = ExecutionLoop(mock_agent)
    pause_response = {
        "content": "still thinking",
        "tool_calls": None,
        "finish_reason": "pause_turn",
    }
    # One more than the cap: the loop restarts without consuming an iteration
    # until the consecutive-pause abort fires.
    loop._call_llm_with_retry = AsyncMock(
        side_effect=[pause_response] * (MAX_CONSECUTIVE_PAUSES + 1)
    )

    result = await loop.run("hello")

    assert result["stop_reason"] == "pause_storm"
    assert result["success"] is False
    assert "pause_turn" in result["content"]
    assert "maximum number of iterations" not in result["content"]
