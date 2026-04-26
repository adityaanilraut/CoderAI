import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coderAI.agent_loop import ExecutionLoop
from coderAI.agent_tracker import AgentInfo, AgentStatus
from coderAI.error_policy import MAX_CONSECUTIVE_ERRORS
from coderAI.history import Session
from coderAI.tool_executor import ToolExecutor


@pytest.mark.asyncio
async def test_denied_tool_skips_pre_hooks() -> None:
    registry = SimpleNamespace(
        get=MagicMock(
            return_value=SimpleNamespace(requires_confirmation=True)
        ),
        execute=AsyncMock(),
    )
    agent = SimpleNamespace(
        auto_approve=False,
        ipc_server=None,
        tools=registry,
        tracker_info=None,
        _sync_tracker=MagicMock(),
    )
    hooks_manager = SimpleNamespace(run_hooks=AsyncMock())
    executor = ToolExecutor(agent)
    executor._confirmation_callback = AsyncMock(return_value=False)

    result = await executor.execute_single_tool(
        {
            "tool_id": "t1",
            "tool_name": "write_file",
            "arguments": {"path": "x", "content": "y"},
            "parse_error": None,
        },
        hooks_data=None,
        hooks_manager=hooks_manager,
    )

    assert result["success"] is False
    assert result["error_code"] == "denied"
    hooks_manager.run_hooks.assert_not_awaited()
    registry.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmation_sets_waiting_for_user_status() -> None:
    info = AgentInfo(
        agent_id="agent_test",
        name="main",
        status=AgentStatus.TOOL_CALL,
        current_tool="write_file",
    )
    seen_statuses = []

    class FakeIPC:
        async def request_tool_approval(self, **kwargs):
            seen_statuses.append(info.status)
            return True

    agent = SimpleNamespace(
        ipc_server=FakeIPC(),
        tracker_info=info,
        _sync_tracker=MagicMock(),
    )
    executor = ToolExecutor(agent)

    approved = await executor._confirmation_callback(
        "write_file",
        {"path": "x", "content": "y"},
        tool_id="t1",
    )

    assert approved is True
    assert seen_statuses == [AgentStatus.WAITING_FOR_USER]
    assert info.status == AgentStatus.TOOL_CALL
    assert info.current_tool == "write_file"


@pytest.mark.asyncio
async def test_all_failed_tool_calls_request_retry() -> None:
    registry = SimpleNamespace(
        get=MagicMock(
            return_value=SimpleNamespace(
                requires_confirmation=False,
                is_read_only=False,
                max_parallel_invocations=0,
            )
        ),
        execute=AsyncMock(return_value={"success": False, "error": "boom"}),
    )
    session = Session(session_id="session_1234567890_deadbeef")
    session.add_message(
        "assistant",
        None,
        tool_calls=[
            {
                "id": "t1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"x"}'},
            }
        ],
    )
    agent = SimpleNamespace(
        auto_approve=True,
        ipc_server=None,
        tools=registry,
        tracker_info=None,
        session=session,
        context_controller=SimpleNamespace(summarize_tool_result=lambda result: result),
        provider=SimpleNamespace(get_model_info=lambda: {"total_tokens": 0}),
        _sync_tracker=MagicMock(),
        _finish_tracker=MagicMock(),
        save_session=MagicMock(),
    )
    hooks_manager = SimpleNamespace(run_hooks=AsyncMock(return_value=[]))
    executor = ToolExecutor(agent)
    messages = session.get_messages_for_api()

    did_error, fatal_res = await executor.orchestrate_tool_calls(
        tool_calls=session.messages[-1].tool_calls,
        messages=messages,
        user_message="inspect the file",
        hooks_data=None,
        hooks_manager=hooks_manager,
        max_consecutive_errors=MAX_CONSECUTIVE_ERRORS,
        current_errors=0,
    )

    assert did_error is True
    assert fatal_res == {"retry": True}
    assert session.messages[-1].role == "tool"
    assert '"success": false' in (session.messages[-1].content or "").lower()


@pytest.mark.asyncio
async def test_tool_result_normalization_wraps_strings() -> None:
    registry = SimpleNamespace(
        get=MagicMock(return_value=SimpleNamespace(requires_confirmation=False)),
        execute=AsyncMock(return_value="boom"),
    )
    agent = SimpleNamespace(
        auto_approve=True,
        ipc_server=None,
        tools=registry,
        tracker_info=None,
        _sync_tracker=MagicMock(),
    )
    hooks_manager = SimpleNamespace(run_hooks=AsyncMock(return_value=[]))
    executor = ToolExecutor(agent)

    result = await executor.execute_single_tool(
        {"tool_id": "t1", "tool_name": "read_file", "arguments": {"path": "x"}, "parse_error": None},
        hooks_data=None,
        hooks_manager=hooks_manager,
    )

    assert result["success"] is False
    assert result["error_code"] == "tool_error"


@pytest.mark.asyncio
async def test_pre_hook_errors_block_tool_execution() -> None:
    registry = SimpleNamespace(
        get=MagicMock(return_value=SimpleNamespace(requires_confirmation=False)),
        execute=AsyncMock(return_value={"success": True}),
    )
    agent = SimpleNamespace(
        auto_approve=True,
        ipc_server=None,
        tools=registry,
        tracker_info=None,
        _sync_tracker=MagicMock(),
    )
    hooks_manager = SimpleNamespace(
        run_hooks=AsyncMock(side_effect=[
            ["[PreToolUse Hook ERROR]: blocked"],
            [],
        ])
    )
    executor = ToolExecutor(agent)

    result = await executor.execute_single_tool(
        {"tool_id": "t1", "tool_name": "write_file", "arguments": {"path": "x", "content": "y"}, "parse_error": None},
        hooks_data=None,
        hooks_manager=hooks_manager,
    )

    assert result["success"] is False
    assert result["error_code"] == "hook_blocked"
    registry.execute.assert_not_awaited()


def test_failed_tool_iterations_accumulate_in_execution_loop() -> None:
    with patch("coderAI.agent.config_manager") as cm:
        from coderAI.config import Config

        cfg = Config(max_iterations=20, budget_limit=0, save_history=False)
        cm.load.return_value = cfg
        cm.load_project_config.return_value = cfg
        from coderAI.agent import Agent

        mock_provider = MagicMock()
        mock_provider.supports_tools.return_value = False
        mock_provider.count_tokens = lambda text: max(1, len(str(text)) // 4)
        mock_provider.get_model_info.return_value = {
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_tokens": 0,
        }

        with patch.object(Agent, "_create_provider", return_value=mock_provider):
            agent = Agent(model="gpt-5.4-mini", streaming=False)

    agent.save_session = MagicMock()
    loop = ExecutionLoop(agent)
    loop._call_llm_with_retry = AsyncMock(
        return_value={
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"x"}'},
                }
            ],
        }
    )

    seen_error_counts = []

    async def fake_orchestrate(
        tool_calls,
        messages,
        user_message,
        hooks_data,
        hooks_manager,
        max_consecutive_errors,
        current_errors,
    ):
        seen_error_counts.append(current_errors)
        if current_errors + 1 >= max_consecutive_errors:
            return True, {
                "content": "Stopped after repeated failed tool iterations.",
                "messages": agent.session.messages,
                "model_info": agent.provider.get_model_info(),
            }
        return True, {"retry": True}

    loop.tool_executor.orchestrate_tool_calls = AsyncMock(side_effect=fake_orchestrate)

    result = asyncio.run(loop.run("hello"))

    assert result["content"] == "Stopped after repeated failed tool iterations."
    assert seen_error_counts == list(range(MAX_CONSECUTIVE_ERRORS))
