import asyncio
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock

import pytest

from coderAI.ipc.jsonrpc_server import (
    IPCServer,
    _cmd_cancel,
    _cmd_clear_context,
    _cmd_handshake,
    _cmd_send_message,
    _cmd_set_model,
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
    )

    await asyncio.gather(
        _cmd_send_message(server, {"text": "first"}),
        _cmd_send_message(server, {"text": "second"}),
    )

    assert max_active == 1


@pytest.mark.asyncio
async def test_set_model_aligns_provider_usage_counters() -> None:
    def _make_provider() -> SimpleNamespace:
        ns = SimpleNamespace(total_input_tokens=0, total_output_tokens=0)

        def _set(*, input_tokens=0, output_tokens=0, **_kw):
            ns.total_input_tokens = max(0, int(input_tokens or 0))
            ns.total_output_tokens = max(0, int(output_tokens or 0))

        ns.set_cumulative_usage = _set
        return ns

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
        _configure_delegate_tool_context=MagicMock(),
        session=None,
    )
    server = SimpleNamespace(agent=agent, emit=MagicMock())

    await _cmd_set_model(server, {"model": "new-model"})

    assert agent.model == "new-model"
    assert agent.provider is new_provider
    assert new_provider.total_input_tokens == 11
    assert new_provider.total_output_tokens == 7
    agent._configure_delegate_tool_context.assert_called_once()


@pytest.mark.asyncio
async def test_clear_context_invokes_session_reset() -> None:
    """``/clear`` now delegates token/cost/provider zeroing to
    ``Agent.create_session`` (which runs ``_reset_session_accounting``).
    This test asserts the orchestration — ``Agent._reset_session_accounting``
    is covered in its own unit test."""
    from coderAI.agent_tracker import AgentInfo, AgentStatus, agent_tracker

    prev_agents = dict(agent_tracker._agents)
    try:
        provider = SimpleNamespace(total_input_tokens=21, total_output_tokens=8)
        context_manager = SimpleNamespace(clear=MagicMock())
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
            context_manager=context_manager,
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

        context_manager.clear.assert_called_once()
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
async def test_handshake_warns_on_protocol_mismatch() -> None:
    server = SimpleNamespace(
        emit=MagicMock(),
        _handshake_done=False,
    )

    await _cmd_handshake(server, {"payload": {"protocolVersion": 1}})

    server.emit.assert_called_once()
    args, kwargs = server.emit.call_args
    assert args == ("warning",)
    assert "Protocol version mismatch" in kwargs["message"]
    assert server._handshake_done is True


@pytest.mark.asyncio
async def test_cancel_resolves_pending_approval_waiters(monkeypatch) -> None:
    fut = asyncio.get_running_loop().create_future()
    server = SimpleNamespace(
        _approval_waiters={"tool_1": fut},
        emit=MagicMock(),
    )
    server._cancel_pending_approvals = lambda reason: IPCServer._cancel_pending_approvals(
        server, reason
    )
    tracker = MagicMock()
    tracker.get_active.return_value = []
    monkeypatch.setattr("coderAI.ipc.jsonrpc_server.agent_tracker", tracker)

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
    server = IPCServer.__new__(IPCServer)
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

    IPCServer.emit_hello(server)

    server.emit.assert_called_once()
    event, payload = server.emit.call_args.args[0], server.emit.call_args.kwargs
    assert event == "hello"
    assert payload["reasoning"] == "medium"


def test_reset_session_accounting_zeros_counters() -> None:
    from coderAI.agent import Agent
    from coderAI.cost import CostTracker

    provider = SimpleNamespace(total_input_tokens=21, total_output_tokens=8)

    def _set(*, input_tokens=0, output_tokens=0, **_kw):
        provider.total_input_tokens = max(0, int(input_tokens or 0))
        provider.total_output_tokens = max(0, int(output_tokens or 0))

    provider.set_cumulative_usage = _set
    # Build an ``Agent``-shaped namespace without invoking __init__ so the
    # test doesn't require real provider config.
    agent = Agent.__new__(Agent)
    agent.provider = provider
    agent.cost_tracker = CostTracker()
    agent.cost_tracker.total_cost_usd = 12.5
    agent.total_prompt_tokens = 21
    agent.total_completion_tokens = 8
    agent.total_tokens = 29
    agent._hooks_approved = {"some-cmd": True}

    Agent._reset_session_accounting(agent)

    assert agent.total_prompt_tokens == 0
    assert agent.total_completion_tokens == 0
    assert agent.total_tokens == 0
    assert provider.total_input_tokens == 0
    assert provider.total_output_tokens == 0
    assert agent.cost_tracker.total_cost_usd == 0.0
    assert agent._hooks_approved == {}
