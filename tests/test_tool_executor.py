import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coderAI.core.agent_loop import ExecutionLoop
from coderAI.core.agent_tracker import AgentInfo, AgentStatus
from coderAI.system.history import Session
from coderAI.core.tool_executor import (
    DOOM_LOOP_HARD_THRESHOLD,
    BatchStatus,
    ToolBatchOutcome,
    ToolExecutor,
)

_UNSET = object()


def _make_tracker_update(info):
    """A stand-in for ``Agent.tracker_update`` bound to a real ``AgentInfo``.

    The executor mutates tracker fields through ``agent.tracker_update`` (Phase
    4.1); SimpleNamespace mock agents need a real one that actually writes.
    """

    def _update(*, status=_UNSET, current_tool=_UNSET, current_task=_UNSET, sync=True):
        if status is not _UNSET:
            info.status = status
        if current_tool is not _UNSET:
            info.current_tool = current_tool
        if current_task is not _UNSET:
            info.current_task = current_task

    return _update


@pytest.mark.asyncio
async def test_denied_tool_skips_pre_hooks() -> None:
    registry = SimpleNamespace(
        get=MagicMock(return_value=SimpleNamespace(requires_confirmation=True)),
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
        tracker_update=_make_tracker_update(info),
        config=SimpleNamespace(approval_timeout_seconds=300),
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

    outcome = await executor.orchestrate_tool_calls(
        tool_calls=session.messages[-1].tool_calls,
        messages=messages,
        user_message="inspect the file",
        hooks_data=None,
        hooks_manager=hooks_manager,
    )

    assert outcome.status is BatchStatus.RETRY
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
        {
            "tool_id": "t1",
            "tool_name": "read_file",
            "arguments": {"path": "x"},
            "parse_error": None,
        },
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
        run_hooks=AsyncMock(
            side_effect=[
                ["[PreToolUse Hook ERROR]: blocked"],
                [],
            ]
        )
    )
    executor = ToolExecutor(agent)

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
    assert result["error_code"] == "hook_blocked"
    registry.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_orchestrate_signals_doom_loop_after_hard_threshold() -> None:
    """Same (tool, args) called >= DOOM_LOOP_HARD_THRESHOLD times must
    surface a BatchStatus.DOOM_LOOP outcome so the agent loop can terminate.

    Real-world repro: a model called the same state-management tool 14+ times
    in one turn. The tool was not read-only, so the cached short-circuit never
    fired; only this hard cap stops it.
    """
    registry = SimpleNamespace(
        get=MagicMock(
            return_value=SimpleNamespace(
                requires_confirmation=False,
                # Mark non-read-only on purpose: the hard cap MUST apply
                # even to mutating tools, which otherwise escape the existing
                # cache check.
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

    last_outcome = None
    for i in range(DOOM_LOOP_HARD_THRESHOLD):
        tool_calls = [
            {
                "id": f"t{i}",
                "type": "function",
                "function": {"name": "manage_tasks", "arguments": '{"action":"show"}'},
            }
        ]
        session.add_message("assistant", None, tool_calls=tool_calls)
        last_outcome = await executor.orchestrate_tool_calls(
            tool_calls=tool_calls,
            messages=session.get_messages_for_api(),
            user_message="complete it",
            hooks_data=None,
            hooks_manager=hooks_manager,
        )

    assert last_outcome is not None
    assert last_outcome.status is BatchStatus.DOOM_LOOP
    assert last_outcome.doom_tool == "manage_tasks"
    assert last_outcome.doom_count == DOOM_LOOP_HARD_THRESHOLD


@pytest.mark.asyncio
async def test_cached_read_only_repeats_trip_doom_loop_hard_threshold() -> None:
    registry = SimpleNamespace(
        get=MagicMock(
            return_value=SimpleNamespace(
                requires_confirmation=False,
                is_read_only=True,
                max_parallel_invocations=0,
            )
        ),
        execute=AsyncMock(return_value={"success": True, "result": "contents"}),
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

    last_outcome = None
    for i in range(DOOM_LOOP_HARD_THRESHOLD):
        tool_calls = [
            {
                "id": f"t{i}",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"README.md"}'},
            }
        ]
        session.add_message("assistant", None, tool_calls=tool_calls)
        last_outcome = await executor.orchestrate_tool_calls(
            tool_calls=tool_calls,
            messages=session.get_messages_for_api(),
            user_message="read it",
            hooks_data=None,
            hooks_manager=hooks_manager,
        )

    assert registry.execute.await_count == 2
    assert last_outcome is not None
    assert last_outcome.status is BatchStatus.DOOM_LOOP
    assert last_outcome.doom_count == DOOM_LOOP_HARD_THRESHOLD


@pytest.mark.asyncio
async def test_recoverable_error_repairs_mid_turn_unpaired_tool_calls() -> None:
    session = Session(session_id="session_1234567890_deadbeef")
    session.add_message(
        "assistant",
        None,
        tool_calls=[
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"x"}'},
            }
        ],
    )
    context_controller = SimpleNamespace(
        inject_context=lambda messages, query=None: messages,
        manage_context_window=AsyncMock(side_effect=lambda messages: messages),
    )
    agent = SimpleNamespace(
        session=session,
        context_controller=context_controller,
        hooks_manager=None,
    )
    loop = ExecutionLoop(agent)

    messages = await loop._handle_recoverable_error(RuntimeError("boom"), 1, "read x")

    tool_messages = [msg for msg in session.messages if msg.role == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0].tool_call_id == "call_1"
    assert any(msg.get("tool_call_id") == "call_1" for msg in messages)


def test_doom_loop_terminates_execution_loop_with_explanatory_message() -> None:
    """End-to-end: when the executor returns BatchStatus.DOOM_LOOP, ExecutionLoop
    must exit immediately (not run to max_iterations) and surface a message
    that names the offending tool."""
    with patch("coderAI.core.agent.config_manager") as cm:
        from coderAI.system.config import Config

        cfg = Config(max_iterations=50, budget_limit=0, save_history=False)
        cm.load.return_value = cfg
        cm.load_project_config.return_value = cfg
        from coderAI.core.agent import Agent

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
                    "function": {"name": "manage_tasks", "arguments": '{"action":"show"}'},
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
        turn=None,
    ):
        nonlocal call_count
        call_count += 1
        if call_count >= DOOM_LOOP_HARD_THRESHOLD:
            return ToolBatchOutcome(
                BatchStatus.DOOM_LOOP, doom_tool="manage_tasks", doom_count=call_count
            )
        return ToolBatchOutcome(BatchStatus.OK)

    loop.tool_executor.orchestrate_tool_calls = AsyncMock(side_effect=fake_orchestrate)

    result = asyncio.run(loop.run("complete it"))

    assert call_count == DOOM_LOOP_HARD_THRESHOLD, (
        "loop should exit immediately on doom-loop, not retry"
    )
    assert "manage_tasks" in result["content"]
    assert "looping" in result["content"].lower() or "loop" in result["content"].lower()
    # Critically: did NOT run to max_iterations.
    assert "maximum number of iterations" not in result["content"]


@pytest.mark.asyncio
async def test_denied_calls_do_not_count_toward_doom_loop_hard_threshold() -> None:
    """Repeated user denials of the same write must not trip the doom-loop stop.

    Before the fix, ``_call_counts`` was incremented for every call regardless
    of result. A user denying ``write_file`` 5× in a turn would surface a
    misleading "stuck in a loop" message instead of a plain "you keep denying".
    """
    fake_tool = SimpleNamespace(
        requires_confirmation=True,
        is_read_only=False,
        max_parallel_invocations=0,
    )
    registry = SimpleNamespace(
        get=MagicMock(return_value=fake_tool),
        execute=AsyncMock(),
    )
    session = Session(session_id="session_1234567890_denyloop")
    agent = SimpleNamespace(
        auto_approve=False,
        ipc_server=None,
        tools=registry,
        tracker_info=None,
        session=session,
        context_controller=SimpleNamespace(summarize_tool_result=lambda r: r),
        provider=SimpleNamespace(get_model_info=lambda: {"total_tokens": 0}),
        _sync_tracker=MagicMock(),
        _finish_tracker=MagicMock(),
        save_session=MagicMock(),
        _tool_approval_allowlist=set(),
    )
    hooks_manager = SimpleNamespace(run_hooks=AsyncMock(return_value=[]))
    executor = ToolExecutor(agent)
    executor._confirmation_callback = AsyncMock(return_value=False)

    last_outcome = None
    for i in range(DOOM_LOOP_HARD_THRESHOLD + 2):
        tool_calls = [
            {
                "id": f"t{i}",
                "type": "function",
                "function": {
                    "name": "write_file",
                    "arguments": '{"path":"a.txt","content":"x"}',
                },
            }
        ]
        session.add_message("assistant", None, tool_calls=tool_calls)
        last_outcome = await executor.orchestrate_tool_calls(
            tool_calls=tool_calls,
            messages=session.get_messages_for_api(),
            user_message="write the file",
            hooks_data=None,
            hooks_manager=hooks_manager,
        )

    # All calls denied → executor returns DENIED, never DOOM_LOOP.
    assert last_outcome is not None
    assert last_outcome.status is BatchStatus.DENIED
    assert "write_file" in last_outcome.denied_tools


@pytest.mark.asyncio
async def test_identical_mutating_calls_are_not_deduplicated() -> None:
    registry = SimpleNamespace(
        get=MagicMock(
            return_value=SimpleNamespace(
                requires_confirmation=False,
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
    tool_calls = [
        {
            "id": f"t{i}",
            "type": "function",
            "function": {"name": "manage_tasks", "arguments": '{"action":"show"}'},
        }
        for i in range(2)
    ]
    session.add_message("assistant", None, tool_calls=tool_calls)

    outcome = await executor.orchestrate_tool_calls(
        tool_calls=tool_calls,
        messages=session.get_messages_for_api(),
        user_message="complete it",
        hooks_data=None,
        hooks_manager=hooks_manager,
    )

    assert outcome.status is BatchStatus.OK
    assert registry.execute.await_count == 2


@pytest.mark.asyncio
async def test_identical_reads_are_not_reused_across_mutation_barrier() -> None:
    read_tool = SimpleNamespace(
        requires_confirmation=False,
        is_read_only=True,
        max_parallel_invocations=0,
    )
    write_tool = SimpleNamespace(
        requires_confirmation=False,
        is_read_only=False,
        max_parallel_invocations=0,
        batch_serialize_by_path=True,
    )
    events = []

    async def _execute(name, **kwargs):
        events.append(name)
        return {"success": True, "result": len(events)}

    registry = SimpleNamespace(
        get=MagicMock(side_effect=lambda name: read_tool if name == "read_file" else write_tool),
        execute=AsyncMock(side_effect=_execute),
    )
    session = Session(session_id="session_1234567890_barrier")
    tool_calls = [
        {
            "id": "r1",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"x.py"}'},
        },
        {
            "id": "w",
            "type": "function",
            "function": {
                "name": "write_file",
                "arguments": '{"path":"x.py","content":"new"}',
            },
        },
        {
            "id": "r2",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"x.py"}'},
        },
    ]
    session.add_message("assistant", None, tool_calls=tool_calls)
    agent = SimpleNamespace(
        auto_approve=True,
        ipc_server=None,
        tools=registry,
        tracker_info=None,
        session=session,
        context_controller=SimpleNamespace(summarize_tool_result=lambda result: result),
        _sync_tracker=MagicMock(),
    )
    hooks_manager = SimpleNamespace(run_hooks=AsyncMock(return_value=[]))

    outcome = await ToolExecutor(agent).orchestrate_tool_calls(
        tool_calls=tool_calls,
        messages=session.get_messages_for_api(),
        user_message="read, write, then read",
        hooks_data=None,
        hooks_manager=hooks_manager,
    )

    assert outcome.status is BatchStatus.OK
    assert events == ["read_file", "write_file", "read_file"]


def test_failed_tool_iterations_accumulate_in_execution_loop() -> None:
    with patch("coderAI.core.agent.config_manager") as cm:
        from coderAI.system.config import Config

        cfg = Config(max_iterations=20, budget_limit=0, save_history=False)
        cm.load.return_value = cfg
        cm.load_project_config.return_value = cfg
        from coderAI.core.agent import Agent

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
        turn=None,
    ):
        nonlocal call_count
        call_count += 1
        # Signal a tool error every time. With separated error counters
        # (consecutive_tool_errors accumulates across iterations), this
        # will terminate after MAX_CONSECUTIVE_ERRORS attempts rather
        # than running to max_iterations.
        return ToolBatchOutcome(BatchStatus.RETRY)

    loop.tool_executor.orchestrate_tool_calls = AsyncMock(side_effect=fake_orchestrate)

    result = asyncio.run(loop.run("hello"))

    # Tool errors accumulate separately from LLM errors and hit the
    # MAX_CONSECUTIVE_ERRORS threshold (default 5).
    from coderAI.system.error_policy import MAX_CONSECUTIVE_ERRORS

    assert "consecutive errors" in result["content"]
    assert call_count == MAX_CONSECUTIVE_ERRORS


def _build_llm_only_agent() -> "Any":
    """Agent with a stubbed provider, for driving ExecutionLoop end-to-end."""
    with patch("coderAI.core.agent.config_manager") as cm:
        from coderAI.system.config import Config

        cfg = Config(max_iterations=50, budget_limit=0, save_history=False)
        cm.load.return_value = cfg
        cm.load_project_config.return_value = cfg
        from coderAI.core.agent import Agent

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
    return agent


def test_in_batch_and_cross_iteration_doom_share_message_format() -> None:
    """Both doom-loop paths (Phase 2.2) surface the *same* stop message.

    The in-batch guard (loop-side, before the executor runs) and the
    cross-iteration guard (executor-side, via ``BatchStatus.DOOM_LOOP``) both
    route through ``loop_guard.doom_message`` now, so a given (tool, count)
    produces byte-identical user-facing text regardless of which path fired.
    """
    from coderAI.core.loop_guard import doom_message

    expected = doom_message("read_file", 3)

    # In-batch: the model emits the same call 3× within ONE response. The guard
    # fires before the executor is ever consulted.
    loop_a = ExecutionLoop(_build_llm_only_agent())
    dup_calls = [
        {
            "id": f"c{i}",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"x"}'},
        }
        for i in range(3)
    ]
    loop_a._call_llm_with_retry = AsyncMock(return_value={"content": None, "tool_calls": dup_calls})
    in_batch_content = asyncio.run(loop_a.run("go"))["content"]

    # Cross-iteration: the executor reports a DOOM_LOOP outcome for one call.
    loop_b = ExecutionLoop(_build_llm_only_agent())
    loop_b._call_llm_with_retry = AsyncMock(
        return_value={
            "content": None,
            "tool_calls": [
                {
                    "id": "c",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"x"}'},
                }
            ],
        }
    )

    async def fake_cross(*_a, **_kw):
        return ToolBatchOutcome(BatchStatus.DOOM_LOOP, doom_tool="read_file", doom_count=3)

    loop_b.tool_executor.orchestrate_tool_calls = AsyncMock(side_effect=fake_cross)
    cross_content = asyncio.run(loop_b.run("go"))["content"]

    assert in_batch_content == expected
    assert cross_content == expected
