import asyncio
import json
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from coderAI.tui.controller import (
    UIBridge,
    _cmd_cancel,
    _cmd_clear_context,
    _cmd_compact_context,
    _cmd_get_plan,
    _cmd_get_state,
    _cmd_get_tasks,
    _cmd_manage_context,
    _cmd_reference,
    _cmd_send_message,
    _cmd_set_model,
    _cmd_set_reasoning,
    _cmd_set_verbosity,
    _cmd_toggle_auto_approve,
    _cmd_tool_approval_resp,
    _serialize_tasks_for_ui,
)


@pytest.mark.asyncio
async def test_send_message_is_serialized_per_server() -> None:
    active = 0
    max_active = 0

    async def process_message(_text: str) -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        active -= 1

    server = SimpleNamespace(
        agent=SimpleNamespace(process_message=process_message),
        _turn_lock=asyncio.Lock(),
        emit=MagicMock(),
        emit_status=MagicMock(),
        emit_ready=MagicMock(),
        tick_iteration=MagicMock(),
    )

    await asyncio.gather(
        _cmd_send_message(server, {"text": "first"}),
        _cmd_send_message(server, {"text": "second"}),
    )

    assert max_active == 1


@pytest.mark.asyncio
async def test_set_model_leaves_agent_totals_and_skips_usage_sync() -> None:
    """Switching models must NOT sync usage into the fresh provider.

    The Agent owns the running token totals and the loop attributes each call's
    usage from the response, so the new provider keeps its zeroed counters and
    the agent's totals are untouched by the switch.
    """

    def _make_provider() -> SimpleNamespace:
        return SimpleNamespace(total_input_tokens=0, total_output_tokens=0)

    old_provider = _make_provider()
    old_provider.total_input_tokens = 3
    old_provider.total_output_tokens = 4
    new_provider = _make_provider()
    agent = SimpleNamespace(
        model="old-model",
        provider=old_provider,
        total_prompt_tokens=11,
        total_completion_tokens=7,
        _create_provider=MagicMock(return_value=new_provider),
        _replace_provider=MagicMock(
            side_effect=lambda: (
                setattr(agent, "provider", new_provider),
                setattr(
                    getattr(agent, "context_controller", SimpleNamespace()),
                    "provider",
                    new_provider,
                ),
            )
        ),
        _configure_delegate_tool_context=MagicMock(),
        context_controller=SimpleNamespace(provider=old_provider),
        session=None,
    )
    server = SimpleNamespace(agent=agent, emit=MagicMock())

    await _cmd_set_model(server, {"model": "new-model"})

    assert agent.model == "new-model"
    assert agent.provider is new_provider
    # Agent totals are the source of truth and are left intact by the switch.
    assert agent.total_prompt_tokens == 11
    assert agent.total_completion_tokens == 7
    # The new provider keeps its own zeroed counters (no sync-from-agent).
    assert new_provider.total_input_tokens == 0
    assert new_provider.total_output_tokens == 0
    assert not hasattr(new_provider, "set_cumulative_usage")
    agent._configure_delegate_tool_context.assert_called_once()


@pytest.mark.asyncio
async def test_clear_context_invokes_session_reset() -> None:
    """``/clear`` now delegates token/cost/provider zeroing to
    ``Agent.create_session`` (which runs ``_reset_session_accounting``).
    This test asserts the orchestration — ``Agent._reset_session_accounting``
    is covered in its own unit test."""
    from coderAI.core.agent_tracker import AgentInfo, AgentStatus, agent_tracker

    prev_agents = dict(agent_tracker._agents)
    try:
        provider = SimpleNamespace(total_input_tokens=21, total_output_tokens=8)
        context_controller = SimpleNamespace(clear=MagicMock())
        cost_tracker = SimpleNamespace(total_cost_usd=12.5)
        main_info = AgentInfo(agent_id="agent_main1234", name="main")
        main_info.status = AgentStatus.THINKING
        main_info.current_task = "old task"
        sub_info = AgentInfo(
            agent_id="agent_sub5678", name="reviewer", parent_id=main_info.agent_id
        )
        agent_tracker._agents.clear()
        agent_tracker._agents[main_info.agent_id] = main_info
        agent_tracker._agents[sub_info.agent_id] = sub_info
        agent = SimpleNamespace(
            session=object(),
            provider=provider,
            context_controller=context_controller,
            total_prompt_tokens=21,
            total_completion_tokens=8,
            total_tokens=29,
            cost_tracker=cost_tracker,
            create_session=MagicMock(),
            tracker_info=main_info,
        )
        server = SimpleNamespace(
            agent=agent,
            emit=MagicMock(),
            emit_status=MagicMock(),
            _turn_lock=asyncio.Lock(),
        )

        await _cmd_clear_context(server, {})

        context_controller.clear.assert_called_once()
        agent.create_session.assert_called_once()
        assert agent.session is None
        assert sub_info.agent_id not in agent_tracker._agents
        assert main_info.agent_id in agent_tracker._agents
        assert main_info.status == AgentStatus.IDLE
        assert main_info.current_task == ""
        server.emit.assert_any_call(
            "agent",
            phase="update",
            info=ANY,
            parentId=main_info.parent_id,
        )
    finally:
        agent_tracker._agents.clear()
        agent_tracker._agents.update(prev_agents)


@pytest.mark.asyncio
async def test_cancel_resolves_pending_approval_waiters(monkeypatch) -> None:
    fut = asyncio.get_running_loop().create_future()
    server = SimpleNamespace(
        _approval_waiters={"tool_1": fut},
        emit=MagicMock(),
    )
    server._cancel_pending_approvals = lambda reason: UIBridge._cancel_pending_approvals(
        server, reason
    )
    tracker = MagicMock()
    tracker.get_active.return_value = []
    monkeypatch.setattr("coderAI.tui.controller.agent_tracker", tracker)

    await _cmd_cancel(server, {})

    assert fut.done()
    assert fut.result() is False
    server.emit.assert_any_call(
        "tool",
        id="tool_1",
        phase="cancelled",
        payload={"reason": "cancelled_by_user"},
    )
    server.emit.assert_any_call(
        "info",
        message="Cancelled 0 active agent(s) and 1 pending approval(s)",
    )


def test_hello_includes_initial_reasoning_state() -> None:
    server = UIBridge.__new__(UIBridge)
    server.agent = SimpleNamespace(
        config=SimpleNamespace(
            context_window=1234,
            budget_limit=1.5,
            reasoning_effort="medium",
        ),
        model="claude",
        provider=SimpleNamespace(),
        auto_approve=False,
    )
    server.emit = MagicMock()

    UIBridge.emit_hello(server)

    server.emit.assert_called_once()
    event, payload = server.emit.call_args.args[0], server.emit.call_args.kwargs
    assert event == "hello"
    assert payload["reasoning"] == "medium"


def test_reset_session_accounting_zeros_counters() -> None:
    from coderAI.core.agent import Agent
    from coderAI.system.cost import CostTracker

    provider = SimpleNamespace(total_input_tokens=21, total_output_tokens=8)

    def _reset() -> None:
        provider.total_input_tokens = 0
        provider.total_output_tokens = 0

    provider.reset_usage = _reset
    # Build an ``Agent``-shaped namespace without invoking __init__ so the
    # test doesn't require real provider config.
    agent = Agent.__new__(Agent)
    agent.provider = provider
    agent.cost_tracker = CostTracker()
    agent.cost_tracker.total_cost_usd = 12.5
    agent.total_prompt_tokens = 21
    agent.total_completion_tokens = 8
    agent.total_tokens = 29
    agent.total_cache_creation_tokens = 5
    agent.total_cache_read_tokens = 7
    agent._hooks_approved = {"some-cmd": True}

    Agent._reset_session_accounting(agent)

    assert agent.total_prompt_tokens == 0
    assert agent.total_completion_tokens == 0
    assert agent.total_tokens == 0
    assert agent.total_cache_creation_tokens == 0
    assert agent.total_cache_read_tokens == 0
    assert provider.total_input_tokens == 0
    assert provider.total_output_tokens == 0
    assert agent.cost_tracker.total_cost_usd == 0.0
    assert agent._hooks_approved == {}


def _make_ipc_server(**agent_overrides) -> SimpleNamespace:
    agent_kwargs = {
        "model": "claude-sonnet-4-6",
        "provider": SimpleNamespace(__class__=type("AnthropicProvider", (), {})),
        "auto_approve": False,
        "config": SimpleNamespace(
            project_root=".",
            max_iterations=50,
            budget_limit=0.0,
            reasoning_effort="none",
        ),
        "context_controller": SimpleNamespace(pinned_files={}),
        "get_context_usage": MagicMock(return_value=(100, 200000)),
        "cost_tracker": SimpleNamespace(get_total_cost=MagicMock(return_value=0.5)),
        "total_prompt_tokens": 10,
        "total_completion_tokens": 5,
        "total_tokens": 15,
        "compact_context": AsyncMock(),
        "persona": None,
    }
    agent_kwargs.update(agent_overrides)
    agent = SimpleNamespace(**agent_kwargs)
    server = SimpleNamespace(
        agent=agent,
        emit=MagicMock(),
        emit_status=MagicMock(),
        emit_ready=MagicMock(),
        _turn_lock=asyncio.Lock(),
        _approval_waiters={},
        _verbosity="normal",
        _iteration=0,
        _session_start_ts=0.0,
        _emit_error=MagicMock(),
    )
    return server


@pytest.mark.asyncio
async def test_tool_approval_resp_resolves_waiter() -> None:
    server = _make_ipc_server()
    fut = asyncio.get_running_loop().create_future()
    server._approval_waiters["tool_99"] = fut

    await _cmd_tool_approval_resp(server, {"toolId": "tool_99", "approve": True})

    assert fut.done()
    assert fut.result() is True
    assert "tool_99" not in server._approval_waiters


@pytest.mark.asyncio
async def test_tool_approval_resp_late_response_emits_warning() -> None:
    server = _make_ipc_server()
    fut = asyncio.get_running_loop().create_future()
    fut.set_result(False)
    server._approval_waiters["tool_late"] = fut

    await _cmd_tool_approval_resp(server, {"toolId": "tool_late", "approve": True})

    server.emit.assert_any_call(
        "warning",
        message="Tool approval response was received too late.",
    )


@pytest.mark.asyncio
async def test_get_state_re_emits_status_agents_and_context() -> None:
    from coderAI.core.agent_tracker import AgentInfo, agent_tracker

    prev_agents = dict(agent_tracker._agents)
    try:
        info = AgentInfo(agent_id="agent_main9999", name="main")
        agent_tracker._agents.clear()
        agent_tracker._agents[info.agent_id] = info
        server = _make_ipc_server(
            context_controller=SimpleNamespace(
                pinned_files={"/tmp/a.py": "print('hi')"},
            ),
        )

        await _cmd_get_state(server, {})

        server.emit_status.assert_called_once()
        server.emit.assert_any_call(
            "agent",
            phase="update",
            info=ANY,
            parentId=info.parent_id,
        )
        server.emit.assert_any_call(
            "context_state",
            files=[{"path": "/tmp/a.py", "size": len("print('hi')")}],
        )
    finally:
        agent_tracker._agents.clear()
        agent_tracker._agents.update(prev_agents)


@pytest.mark.asyncio
async def test_compact_context_success_and_failure() -> None:
    ok_server = _make_ipc_server()
    await _cmd_compact_context(ok_server, {})
    ok_server.agent.compact_context.assert_awaited_once()
    ok_server.emit.assert_any_call("success", message="Context compacted")
    ok_server.emit_status.assert_called_once()

    fail_server = _make_ipc_server(
        compact_context=AsyncMock(side_effect=RuntimeError("boom")),
    )
    await _cmd_compact_context(fail_server, {})
    fail_server._emit_error.assert_called_once_with(
        "internal",
        "Compaction failed: boom",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("topic", "expected_substring"),
    [
        ("version", "CoderAI"),
        ("models", "claude"),
        ("cost", "Cost"),
        ("config", "config"),
    ],
)
async def test_reference_emits_info_for_known_topics(topic, expected_substring) -> None:
    server = _make_ipc_server()
    with patch(
        "coderAI.tui.commands._resolve_reference_text",
        return_value=f"Reference output for {topic}: {expected_substring}",
    ):
        await _cmd_reference(server, {"topic": topic})

    server.emit.assert_called_once_with(
        "info",
        message=f"Reference output for {topic}: {expected_substring}",
    )


@pytest.mark.asyncio
async def test_reference_missing_topic_emits_warning() -> None:
    server = _make_ipc_server()
    await _cmd_reference(server, {"topic": ""})
    server.emit.assert_called_once()
    assert server.emit.call_args.args[0] == "warning"


@pytest.mark.asyncio
async def test_toggle_auto_approve_flips_flag_and_patches_session() -> None:
    server = _make_ipc_server(auto_approve=False)
    server.agent._configure_delegate_tool_context = MagicMock()

    await _cmd_toggle_auto_approve(server, {})

    assert server.agent.auto_approve is True
    server.emit.assert_any_call("session_patch", autoApprove=True)
    server.agent._configure_delegate_tool_context.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", ["high", "medium", "low", "none"])
async def test_set_reasoning_valid_efforts(effort) -> None:
    server = _make_ipc_server()
    await _cmd_set_reasoning(server, {"effort": effort})
    assert server.agent.config.reasoning_effort == effort
    server.emit.assert_called_once_with("session_patch", reasoning=effort)


@pytest.mark.asyncio
async def test_set_reasoning_invalid_effort_emits_warning() -> None:
    server = _make_ipc_server()
    await _cmd_set_reasoning(server, {"effort": "turbo"})
    server.emit.assert_called_once()
    assert server.emit.call_args.args[0] == "warning"


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["quiet", "normal", "verbose"])
async def test_set_verbosity_updates_filter(level) -> None:
    server = _make_ipc_server()
    await _cmd_set_verbosity(server, {"level": level})
    assert server._verbosity == level


@pytest.mark.asyncio
async def test_get_plan_emits_plan_card_and_info(tmp_path, monkeypatch) -> None:
    dot_coderai = tmp_path / ".coderAI"
    dot_coderai.mkdir()
    plan = {
        "title": "Ship feature",
        "current_step": 0,
        "steps": [
            {"description": "Design", "status": "pending"},
            {"description": "Implement", "status": "pending"},
        ],
    }
    (dot_coderai / "current_plan.json").write_text(json.dumps(plan), encoding="utf-8")

    server = _make_ipc_server(
        config=SimpleNamespace(project_root=str(tmp_path), max_iterations=50, budget_limit=0.0),
    )
    monkeypatch.setattr(
        "coderAI.system.project_layout.read_current_plan",
        lambda *_args, **_kwargs: plan,
    )

    await _cmd_get_plan(server, {})

    server.emit.assert_any_call("plan_card", plan=ANY)
    info_calls = [c for c in server.emit.call_args_list if c.args[0] == "info"]
    assert info_calls
    assert "Ship feature" in info_calls[0].kwargs["message"]


@pytest.mark.asyncio
async def test_manage_context_actions() -> None:
    context_controller = MagicMock()
    context_controller.add_file = MagicMock(return_value=True)
    context_controller.remove_file = MagicMock(return_value=True)
    context_controller.pinned_files = {"/path/to/file.py": "content"}

    server = _make_ipc_server(context_controller=context_controller)

    # 1. Test add success
    await _cmd_manage_context(server, {"action": "add", "path": "/path/to/file.py"})
    context_controller.add_file.assert_called_with("/path/to/file.py")
    server.emit.assert_any_call("success", message="Added /path/to/file.py to pinned context.")
    server.emit.assert_any_call("context_state", files=[{"path": "/path/to/file.py", "size": 7}])

    # Reset mocks
    context_controller.add_file.reset_mock()
    server.emit.reset_mock()

    # 2. Test add failure
    context_controller.add_file.return_value = False
    await _cmd_manage_context(server, {"action": "add", "path": "/path/to/file.py"})
    context_controller.add_file.assert_called_with("/path/to/file.py")
    server.emit.assert_any_call(
        "warning",
        message="Failed to add /path/to/file.py to context (may be too large or invalid).",
    )

    # Reset mocks
    context_controller.add_file.reset_mock()
    server.emit.reset_mock()

    # 3. Test add missing path
    await _cmd_manage_context(server, {"action": "add"})
    server.emit.assert_any_call("warning", message="Path required to add to context.")

    # Reset mocks
    server.emit.reset_mock()

    # 4. Test remove success
    await _cmd_manage_context(server, {"action": "remove", "path": "/path/to/file.py"})
    context_controller.remove_file.assert_called_with("/path/to/file.py")
    server.emit.assert_any_call("success", message="Removed /path/to/file.py from context.")

    # Reset mocks
    context_controller.remove_file.reset_mock()
    server.emit.reset_mock()

    # 5. Test remove failure
    context_controller.remove_file.return_value = False
    await _cmd_manage_context(server, {"action": "remove", "path": "/path/to/file.py"})
    context_controller.remove_file.assert_called_with("/path/to/file.py")
    server.emit.assert_any_call(
        "warning", message="Failed to remove /path/to/file.py from context."
    )

    # Reset mocks
    context_controller.remove_file.reset_mock()
    server.emit.reset_mock()

    # 6. Test remove missing path
    await _cmd_manage_context(server, {"action": "remove"})
    server.emit.assert_any_call("warning", message="Path required to remove from context.")


def test_serialize_tasks_for_ui_groups_and_caps_completed() -> None:
    tasks = [
        {"id": 1, "title": "A", "priority": "high", "status": "in_progress"},
        {"id": 2, "title": "B", "priority": "low", "status": "pending"},
        {"id": 3, "title": "C", "priority": "medium", "status": "completed"},
        {"id": 4, "title": "D", "priority": "medium", "status": "completed"},
        {"id": 5, "title": "E", "priority": "medium", "status": "completed"},
        {"id": 6, "title": "F", "priority": "medium", "status": "completed"},
        {"id": 7, "title": "G", "priority": "medium", "status": "completed"},
        {"id": 8, "title": "H", "priority": "medium", "status": "completed"},
    ]
    payload = _serialize_tasks_for_ui(tasks)

    assert payload["total"] == 8
    assert payload["summary"] == "1 in-progress, 1 pending, 6 completed"
    assert len(payload["inProgress"]) == 1
    assert payload["inProgress"][0]["title"] == "A"
    assert len(payload["pending"]) == 1
    assert len(payload["completed"]) == 5
    assert payload["completed"][-1]["title"] == "H"


@pytest.mark.asyncio
async def test_get_tasks_emits_tasks_card() -> None:
    server = SimpleNamespace(
        agent=SimpleNamespace(config=SimpleNamespace(project_root=".")),
        emit=MagicMock(),
        _emit_tasks_from_disk=MagicMock(),
    )

    await _cmd_get_tasks(server, {})

    server._emit_tasks_from_disk.assert_called_once()
