from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from coderAI.core.agent_loop import ExecutionLoop
from coderAI.core.tool_executor import BatchStatus, ToolBatchOutcome
from coderAI.core.turn import TurnContext
from coderAI.system.history import Session


@pytest.mark.asyncio
async def test_mcp_topology_call_refreshes_prompt_and_schemas_before_next_iteration():
    session = Session(session_id="session_1000_aaaaaaaa", model="test")
    session.add_message("system", "old prompt")
    session.add_message("user", "connect")

    context_controller = SimpleNamespace(
        inject_context=lambda messages, query: messages,
        manage_context_window=AsyncMock(side_effect=lambda messages: messages),
    )
    agent = SimpleNamespace(
        hooks_manager=MagicMock(),
        session=session,
        config=SimpleNamespace(budget_limit=0, continue_loop_on_deny=True),
        context_controller=context_controller,
        tracker_info=None,
        _cached_system_prompt="old prompt",
        _tool_schemas_dirty=False,
    )

    def refresh_prompt() -> None:
        session.messages[0].content = "new prompt"

    agent._refresh_session_system_prompt = MagicMock(side_effect=refresh_prompt)
    loop = ExecutionLoop(agent)
    loop.tool_executor.orchestrate_tool_calls = AsyncMock(
        return_value=ToolBatchOutcome(status=BatchStatus.OK)
    )
    state = TurnContext(
        user_message="connect",
        messages=session.get_messages_for_api(),
        tool_schemas=[{"type": "function", "function": {"name": "mcp_connect"}}],
    )

    result = await loop._handle_tools_phase(
        state,
        {
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "mcp_connect", "arguments": "{}"},
                }
            ]
        },
    )

    assert result is None
    assert agent._tool_schemas_dirty is True
    assert agent._cached_system_prompt is None
    agent._refresh_session_system_prompt.assert_called_once_with()
    assert state.messages[0]["content"] == "new prompt"


@pytest.mark.asyncio
async def test_schema_dirty_flag_survives_new_execution_loop(mock_agent):
    """Health/reconnect dirty bit is owned by Agent, not the per-turn loop."""
    mock_agent._tool_schemas_dirty = True
    mock_agent._mcp_health_check_counter = 0
    schemas = [{"type": "function", "function": {"name": "read_file"}}]
    mock_agent.tools.get_schemas.return_value = schemas

    loop = ExecutionLoop(mock_agent)
    state = TurnContext(
        user_message="hi",
        messages=[{"role": "user", "content": "hi"}],
        tool_schemas=[],
    )
    loop._call_llm_with_retry = AsyncMock(
        return_value={"content": "ok", "tool_calls": None, "finish_reason": "stop"}
    )
    loop._get_tool_schemas = MagicMock(return_value=schemas)

    await loop._handle_llm_phase(state)

    assert mock_agent._tool_schemas_dirty is False
    assert state.tool_schemas == schemas
    loop._get_tool_schemas.assert_called_once_with("hi", warm_tool_names=set())
