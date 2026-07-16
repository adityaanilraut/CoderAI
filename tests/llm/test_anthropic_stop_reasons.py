import pytest
from unittest.mock import AsyncMock, MagicMock

from coderAI.core.agent_loop import ExecutionLoop
from coderAI.core.agent import Agent
from coderAI.core.services import services_scope
from coderAI.system.history import Session


@pytest.fixture
def mock_agent():
    agent = MagicMock(spec=Agent)
    agent.session = Session(session_id="test_session")
    agent.config = MagicMock()
    agent.config.max_iterations = 5
    agent.config.max_iterations_hard_cap = 200
    agent.config.budget_limit = 0
    agent.cost_tracker = MagicMock()
    agent.cost_tracker.get_total_cost.return_value = 0
    agent.provider = AsyncMock()
    agent.provider.supports_tools = MagicMock(return_value=True)
    agent.provider.get_model_info = MagicMock(return_value={})
    agent.provider.clean_messages = MagicMock(side_effect=lambda msgs: msgs)
    agent.tools = MagicMock()
    agent.tools.get_schemas.return_value = []
    agent.context_controller = MagicMock()
    agent.context_controller.inject_context = lambda msgs, query: msgs
    agent.context_controller.strip_internal_markers = lambda msgs: msgs
    agent.context_controller.manage_context_window = AsyncMock(side_effect=lambda msgs: msgs)
    agent._context_controller = MagicMock()
    agent._mcp_initialized = True
    agent._workspace_trust_checked = True
    agent.tracker_info = None
    agent.total_prompt_tokens = 0
    agent.total_completion_tokens = 0
    agent.total_tokens = 0
    agent.total_cache_creation_tokens = 0
    agent.total_cache_read_tokens = 0
    agent.hooks_manager = MagicMock()
    agent.hooks_manager.load_hooks = MagicMock(return_value={})
    agent.hooks_manager.run_hooks = AsyncMock()
    agent.model = "claude"
    agent.streaming = False
    return agent


@pytest.mark.asyncio
async def test_anthropic_refusal(mock_agent):
    loop = ExecutionLoop(mock_agent)

    # Mock provider returning refusal
    mock_agent.provider.chat.return_value = {
        "choices": [
            {"message": {"content": "I cannot help with that."}, "finish_reason": "refusal"}
        ],
        "usage": {},
    }

    # Swap the event emitter core sees via the service container (Phase 4.4).
    mock_emitter = MagicMock()
    with services_scope(events=mock_emitter):
        result = await loop.run("build a malware")

    # It should exit without looping and return the refusal text
    assert result["content"] == "I cannot help with that."
    assert mock_emitter.emit.call_count >= 1
    warning_calls = [c for c in mock_emitter.emit.call_args_list if c[0][0] == "agent_warning"]
    assert "refused this request" in warning_calls[0][1]["message"]

    # Provider should be called exactly once
    assert mock_agent.provider.chat.call_count == 1


@pytest.mark.asyncio
async def test_anthropic_pause_turn(mock_agent):
    loop = ExecutionLoop(mock_agent)

    # Provider returns pause_turn first, then end_turn (stop)
    mock_agent.provider.chat.side_effect = [
        {
            "choices": [{"message": {"content": "Thinking..."}, "finish_reason": "pause_turn"}],
            "usage": {},
        },
        {"choices": [{"message": {"content": " Done."}, "finish_reason": "stop"}], "usage": {}},
    ]

    result = await loop.run("solve complex math")

    # Both parts of the content should be accumulated and returned
    assert result["content"] == "Thinking...\n\nDone."
    # Provider should be called twice
    assert mock_agent.provider.chat.call_count == 2

    # Second LLM call must include the pause_turn assistant message in history
    second_call_messages = mock_agent.provider.chat.call_args_list[1][0][0]
    assistant_contents = [
        m.get("content")
        for m in second_call_messages
        if m.get("role") == "assistant" and m.get("content")
    ]
    assert "Thinking..." in assistant_contents
