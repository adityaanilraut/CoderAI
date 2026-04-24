import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from coderAI.ipc.jsonrpc_server import (
    _cmd_clear_context,
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
    old_provider = SimpleNamespace(total_input_tokens=3, total_output_tokens=4)
    new_provider = SimpleNamespace(total_input_tokens=0, total_output_tokens=0)
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
    provider = SimpleNamespace(total_input_tokens=21, total_output_tokens=8)
    context_manager = SimpleNamespace(clear=MagicMock())
    cost_tracker = SimpleNamespace(total_cost_usd=12.5)
    agent = SimpleNamespace(
        session=object(),
        provider=provider,
        context_manager=context_manager,
        total_prompt_tokens=21,
        total_completion_tokens=8,
        total_tokens=29,
        cost_tracker=cost_tracker,
        create_session=MagicMock(),
    )
    server = SimpleNamespace(
        agent=agent,
        emit=MagicMock(),
        emit_status=MagicMock(),
    )

    await _cmd_clear_context(server, {})

    context_manager.clear.assert_called_once()
    agent.create_session.assert_called_once()
    assert agent.session is None


def test_reset_session_accounting_zeros_counters() -> None:
    from coderAI.agent import Agent
    from coderAI.cost import CostTracker

    provider = SimpleNamespace(total_input_tokens=21, total_output_tokens=8)
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
