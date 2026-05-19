"""Tests for one-shot length finish_reason recovery in the execution loop."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from coderAI.agent_loop import ExecutionLoop


@pytest.fixture
def mock_agent():
    agent = MagicMock()
    agent.session = MagicMock()
    agent.session.messages = []
    agent.session.add_message = MagicMock()
    agent.session.get_messages_for_api = MagicMock(return_value=[])
    agent.config = MagicMock()
    agent.config.max_iterations = 10
    agent.config.max_iterations_hard_cap = 200
    agent.config.budget_limit = 0
    agent.config.continue_loop_on_deny = True
    agent.cost_tracker = MagicMock()
    agent.cost_tracker.get_total_cost.return_value = 0
    agent.provider = MagicMock()
    agent.provider.get_model_info.return_value = {}
    agent.tools = MagicMock()
    agent.tools.get_schemas.return_value = []
    agent.context_controller = MagicMock()
    agent.context_controller.inject_context = lambda msgs, cm, query=None: msgs
    agent.context_controller.manage_context_window = AsyncMock(side_effect=lambda msgs: msgs)
    agent.context_manager = MagicMock()
    agent._assistant_reply_parts = []
    agent.tracker_info = None
    agent._register_tracker = MagicMock()
    agent._sync_tracker = MagicMock()
    agent._finish_tracker = MagicMock()
    agent.save_session = MagicMock()
    agent.read_cache = None
    agent.hooks_manager = MagicMock()
    agent.hooks_manager.load_hooks.return_value = None
    return agent


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
    loop.tool_executor.orchestrate_tool_calls = AsyncMock(return_value=(False, None))

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
    loop.tool_executor.orchestrate_tool_calls = AsyncMock(return_value=(False, None))

    result = await loop.run("do work")

    assert loop._call_llm_with_retry.await_count == 3
    assert "max_tokens" in result["content"].lower()
