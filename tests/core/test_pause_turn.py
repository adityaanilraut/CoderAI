"""Tests for pause_turn message history sync in the execution loop."""

from unittest.mock import AsyncMock

import pytest

from coderAI.core.agent_loop import ExecutionLoop
from coderAI.core.tool_executor import BatchStatus, ToolBatchOutcome


@pytest.mark.asyncio
async def test_pause_turn_refreshes_messages_from_session(mock_agent):
    """After pause_turn, the in-loop messages list must match session history."""
    # Anthropic preserves the paused tool_use blocks on resume, so the loop
    # must keep them — signalled by the provider capability flag (no module
    # sniffing).
    mock_agent.provider.preserves_tool_calls_on_pause = True
    loop = ExecutionLoop(mock_agent)
    shared_messages: list = []

    async def fake_prepare(_user_message):
        shared_messages.clear()
        shared_messages.extend(mock_agent.session.get_messages_for_api())
        return shared_messages

    loop._prepare_messages = fake_prepare

    pause_response = {
        "content": "thinking aloud",
        "tool_calls": [
            {
                "id": "call_pause",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
            }
        ],
        "finish_reason": "pause_turn",
    }
    final_response = {"content": "done", "finish_reason": "stop"}

    loop._call_llm_with_retry = AsyncMock(side_effect=[pause_response, final_response])
    loop.tool_executor.orchestrate_tool_calls = AsyncMock(
        return_value=ToolBatchOutcome(BatchStatus.OK)
    )

    await loop.run("hello")

    assert len(mock_agent.session.messages) == 3
    assert mock_agent.session.messages[1].role == "assistant"
    assert mock_agent.session.messages[1].content == "thinking aloud"
    assert mock_agent.session.messages[1].tool_calls is not None
    assert shared_messages == mock_agent.session.get_messages_for_api()
    assert loop._call_llm_with_retry.await_count == 2


@pytest.mark.asyncio
async def test_pause_turn_strips_tool_calls_for_non_preserving_provider(mock_agent):
    """A provider that does NOT replay paused tool_use blocks must have them
    stripped from history before the loop resumes (OpenAI-compatible behavior)."""
    mock_agent.provider.preserves_tool_calls_on_pause = False
    loop = ExecutionLoop(mock_agent)
    shared_messages: list = []

    async def fake_prepare(_user_message):
        shared_messages.clear()
        shared_messages.extend(mock_agent.session.get_messages_for_api())
        return shared_messages

    loop._prepare_messages = fake_prepare

    pause_response = {
        "content": "thinking aloud",
        "tool_calls": [
            {
                "id": "call_pause",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
            }
        ],
        "finish_reason": "pause_turn",
    }
    final_response = {"content": "done", "finish_reason": "stop"}

    loop._call_llm_with_retry = AsyncMock(side_effect=[pause_response, final_response])
    loop.tool_executor.orchestrate_tool_calls = AsyncMock(
        return_value=ToolBatchOutcome(BatchStatus.OK)
    )

    await loop.run("hello")

    assert mock_agent.session.messages[1].role == "assistant"
    assert mock_agent.session.messages[1].tool_calls is None
    assert loop._call_llm_with_retry.await_count == 2
