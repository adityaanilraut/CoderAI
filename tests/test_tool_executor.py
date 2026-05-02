import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coderAI.agent_loop import ExecutionLoop
from coderAI.agent_tracker import AgentInfo, AgentStatus
from coderAI.history import Session
from coderAI.tool_executor import DOOM_LOOP_HARD_THRESHOLD, ToolExecutor


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
        _tool_approval_allowlist=set(),
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


@pytest.mark.asyncio
async def test_orchestrate_signals_doom_loop_after_hard_threshold() -> None:
    """Same (tool, args) called >= DOOM_LOOP_HARD_THRESHOLD times must
    surface the _doom_loop_stop signal so the agent loop can terminate.

    Real-world repro: gpt-5.4-mini called `plan action=show` 14+ times in
    one turn. The plan tool isn't is_read_only, so the cached short-circuit
    never fired — only this hard cap stops it.
    """
    registry = SimpleNamespace(
        get=MagicMock(
            return_value=SimpleNamespace(
                requires_confirmation=False,
                # Mark non-read-only on purpose: the hard cap MUST apply
                # even to mutating tools, otherwise tools like `plan` (which
                # has is_read_only=False) escape the existing cache check.
                is_read_only=False,
                max_parallel_invocations=0,
            )
        ),
        execute=AsyncMock(return_value={"success": True, "result": "ok"}),
    )
    session = Session(session_id="session_1234567890_deadbeef")
    agent = SimpleNamespace(
        auto_approve=True,
        ipc_server=None,
        tools=registry,
        tracker_info=None,
        session=session,
        context_controller=SimpleNamespace(summarize_tool_result=lambda r: r),
        provider=SimpleNamespace(get_model_info=lambda: {"total_tokens": 0}),
        _sync_tracker=MagicMock(),
        _finish_tracker=MagicMock(),
        save_session=MagicMock(),
    )
    hooks_manager = SimpleNamespace(run_hooks=AsyncMock(return_value=[]))
    executor = ToolExecutor(agent)

    last_did_error = False
    last_fatal_res = None
    for i in range(DOOM_LOOP_HARD_THRESHOLD):
        tool_calls = [{
            "id": f"t{i}",
            "type": "function",
            "function": {"name": "plan", "arguments": '{"action":"show"}'},
        }]
        session.add_message("assistant", None, tool_calls=tool_calls)
        last_did_error, last_fatal_res = await executor.orchestrate_tool_calls(
            tool_calls=tool_calls,
            messages=session.get_messages_for_api(),
            user_message="complete it",
            hooks_data=None,
            hooks_manager=hooks_manager,
        )

    assert last_did_error is True
    assert isinstance(last_fatal_res, dict)
    assert last_fatal_res.get("_doom_loop_stop") is True
    assert last_fatal_res["tool_name"] == "plan"
    assert last_fatal_res["count"] == DOOM_LOOP_HARD_THRESHOLD


def test_doom_loop_terminates_execution_loop_with_explanatory_message() -> None:
    """End-to-end: when the executor signals _doom_loop_stop, ExecutionLoop
    must exit immediately (not run to max_iterations) and surface a message
    that names the offending tool."""
    with patch("coderAI.agent.config_manager") as cm:
        from coderAI.config import Config

        cfg = Config(max_iterations=50, budget_limit=0, save_history=False)
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
                    "id": "call_plan",
                    "type": "function",
                    "function": {"name": "plan", "arguments": '{"action":"show"}'},
                }
            ],
        }
    )

    call_count = 0

    async def fake_orchestrate(
        tool_calls, messages, user_message, hooks_data, hooks_manager,
    ):
        nonlocal call_count
        call_count += 1
        if call_count >= DOOM_LOOP_HARD_THRESHOLD:
            return True, {
                "_doom_loop_stop": True,
                "tool_name": "plan",
                "count": call_count,
            }
        return False, None

    loop.tool_executor.orchestrate_tool_calls = AsyncMock(side_effect=fake_orchestrate)

    result = asyncio.run(loop.run("complete it"))

    assert call_count == DOOM_LOOP_HARD_THRESHOLD, (
        "loop should exit immediately on _doom_loop_stop, not retry"
    )
    assert "plan" in result["content"]
    assert "looping" in result["content"].lower() or "loop" in result["content"].lower()
    # Critically: did NOT run to max_iterations.
    assert "maximum number of iterations" not in result["content"]


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

    call_count = 0

    async def fake_orchestrate(
        tool_calls,
        messages,
        user_message,
        hooks_data,
        hooks_manager,
    ):
        nonlocal call_count
        call_count += 1
        # Always signal a tool error. The loop's consecutive_errors counter
        # resets on each successful LLM call, so tool failures during a
        # working LLM session run until max_iterations is exhausted.
        return True, {"retry": True}

    loop.tool_executor.orchestrate_tool_calls = AsyncMock(side_effect=fake_orchestrate)

    result = asyncio.run(loop.run("hello"))

    assert "maximum number of iterations" in result["content"]
    assert call_count == cfg.max_iterations
