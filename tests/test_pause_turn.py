"""Tests for pause_turn message history sync in the execution loop."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from coderAI.core.agent_loop import ExecutionLoop
from coderAI.system.history import Session


@pytest.fixture
def mock_agent():
    agent = MagicMock()
    agent.session = Session(session_id="test")
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


@pytest.mark.asyncio
async def test_pause_turn_refreshes_messages_from_session(mock_agent):
    """After pause_turn, the in-loop messages list must match session history."""
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
    loop.tool_executor.orchestrate_tool_calls = AsyncMock(return_value=(False, None))

    await loop.run("hello")

    assert len(mock_agent.session.messages) == 3
    assert mock_agent.session.messages[1].role == "assistant"
    assert mock_agent.session.messages[1].content == "thinking aloud"
    assert mock_agent.session.messages[1].tool_calls is not None
    assert shared_messages == mock_agent.session.get_messages_for_api()
    assert loop._call_llm_with_retry.await_count == 2
