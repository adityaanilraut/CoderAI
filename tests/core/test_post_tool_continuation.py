"""Regression coverage for empty model responses after tool execution."""

from unittest.mock import AsyncMock

import pytest

from coderAI.core.agent_loop import ExecutionLoop
from coderAI.core.tool_executor import BatchStatus, ToolBatchOutcome


def _tool_call(call_id: str, script: str) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "run_applescript",
            "arguments": f'{{"script":"{script}"}}',
        },
    }


@pytest.mark.asyncio
async def test_empty_post_tool_response_continues_without_user_message(mock_agent):
    loop = ExecutionLoop(mock_agent)
    seen_messages = []
    responses = [
        {
            "content": None,
            "tool_calls": [_tool_call("play", "play")],
            "finish_reason": "tool_calls",
        },
        {"content": None, "tool_calls": None, "finish_reason": "stop"},
        {
            "content": None,
            "tool_calls": [_tool_call("verify", "current track")],
            "finish_reason": "tool_calls",
        },
        {
            "content": 'Now playing Taylor Swift - "The Fate of Ophelia" on Spotify.',
            "tool_calls": None,
            "finish_reason": "stop",
        },
    ]

    async def fake_call(messages, _tool_schemas):
        seen_messages.append([dict(message) for message in messages])
        return responses[len(seen_messages) - 1]

    loop._call_llm_with_retry = AsyncMock(side_effect=fake_call)
    loop.tool_executor.orchestrate_tool_calls = AsyncMock(
        return_value=ToolBatchOutcome(BatchStatus.OK)
    )

    result = await loop.run("play Taylor Swift on Spotify")

    assert "Now playing Taylor Swift" in result["content"]
    assert loop._call_llm_with_retry.await_count == 4
    assert any(
        message.get("role") == "system"
        and "without waiting for another user message" in message.get("content", "")
        for message in seen_messages[2]
    )


@pytest.mark.asyncio
async def test_repeated_empty_post_tool_responses_return_visible_fallback(mock_agent):
    loop = ExecutionLoop(mock_agent)
    loop._call_llm_with_retry = AsyncMock(
        side_effect=[
            {
                "content": None,
                "tool_calls": [_tool_call("play", "play")],
                "finish_reason": "tool_calls",
            },
            {"content": None, "tool_calls": None, "finish_reason": "stop"},
            {"content": None, "tool_calls": None, "finish_reason": "stop"},
            {"content": None, "tool_calls": None, "finish_reason": "stop"},
        ]
    )
    loop.tool_executor.orchestrate_tool_calls = AsyncMock(
        return_value=ToolBatchOutcome(BatchStatus.OK)
    )

    result = await loop.run("play Taylor Swift on Spotify")

    assert result["content"] == (
        "The requested tool action completed, but no final details were returned."
    )
    assert loop._call_llm_with_retry.await_count == 4


@pytest.mark.asyncio
async def test_empty_gap_after_later_tool_batch_also_continues(mock_agent):
    """Each successful tool batch should re-arm one empty-response recovery."""
    loop = ExecutionLoop(mock_agent)
    responses = [
        {
            "content": None,
            "tool_calls": [_tool_call("play", "play")],
            "finish_reason": "tool_calls",
        },
        {"content": None, "tool_calls": None, "finish_reason": "stop"},
        {
            "content": None,
            "tool_calls": [_tool_call("verify", "current track")],
            "finish_reason": "tool_calls",
        },
        {"content": None, "tool_calls": None, "finish_reason": "stop"},
        {
            "content": "Verified playback is active.",
            "tool_calls": None,
            "finish_reason": "stop",
        },
    ]
    loop._call_llm_with_retry = AsyncMock(side_effect=responses)
    loop.tool_executor.orchestrate_tool_calls = AsyncMock(
        return_value=ToolBatchOutcome(BatchStatus.OK)
    )

    result = await loop.run("play and verify Spotify")

    assert "Verified playback is active." in result["content"]
    assert loop._call_llm_with_retry.await_count == 5
