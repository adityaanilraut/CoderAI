"""Focused coverage for persisted accounting and provider replacement wiring."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coderAI.core.agent import Agent
from coderAI.system.config import Config
from coderAI.system.cost import CostTracker
from coderAI.system.history import Session


def _bare_agent(session: Session) -> Agent:
    agent = Agent.__new__(Agent)
    agent.model = session.model
    agent.session = None
    agent.provider = SimpleNamespace(reset_usage=MagicMock())
    agent.cost_tracker = CostTracker()
    agent.total_prompt_tokens = 99
    agent.total_completion_tokens = 99
    agent.total_tokens = 198
    agent.total_cache_creation_tokens = 99
    agent.total_cache_read_tokens = 99
    agent._hooks_approved = {}
    agent._refresh_session_system_prompt = MagicMock()
    agent.config = SimpleNamespace(save_history=True)
    agent._save_executor = None
    agent._pending_saves = set()
    return agent


def test_load_session_restores_accounting_and_save_snapshots_live_totals() -> None:
    session = Session(
        session_id="session_1_abcdef12",
        model="claude-sonnet-4-6",
        prompt_tokens=120,
        completion_tokens=30,
        total_tokens=150,
        cache_creation_tokens=11,
        cache_read_tokens=22,
        total_cost_usd=1.75,
    )
    agent = _bare_agent(session)
    history = SimpleNamespace(
        load_session=MagicMock(return_value=session),
        save_session_data=MagicMock(),
    )

    with patch(
        "coderAI.core.agent_session.get_services",
        return_value=SimpleNamespace(history=history),
    ):
        loaded = agent.load_session(session.session_id)
        agent.total_prompt_tokens += 5
        agent.total_completion_tokens += 2
        agent.total_tokens += 7
        agent.cost_tracker.total_cost_usd += 0.25
        agent.save_session()

    assert loaded is session
    assert agent.total_prompt_tokens == 125
    assert agent.total_completion_tokens == 32
    assert agent.total_cache_creation_tokens == 11
    assert agent.total_cache_read_tokens == 22
    assert agent.cost_tracker.get_total_cost() == pytest.approx(2.0)
    snapshot = history.save_session_data.call_args.args[0]
    assert snapshot["prompt_tokens"] == 125
    assert snapshot["completion_tokens"] == 32
    assert snapshot["total_tokens"] == 157
    assert snapshot["total_cost_usd"] == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_provider_replacement_rewires_skills_and_closes_old_provider() -> None:
    agent = Agent.__new__(Agent)
    old_provider = SimpleNamespace(close=AsyncMock())
    new_provider = SimpleNamespace()
    agent.provider = old_provider
    agent._context_controller = SimpleNamespace(provider=old_provider)
    agent.skill_manager = SimpleNamespace(provider=old_provider)
    agent._create_provider = MagicMock(return_value=new_provider)

    agent._replace_provider()
    await asyncio.sleep(0)

    assert agent.provider is new_provider
    assert agent._context_controller.provider is new_provider
    assert agent.skill_manager.provider is new_provider
    old_provider.close.assert_awaited_once()


def test_skill_auto_detection_and_retention_defaults() -> None:
    config = Config()

    assert config.auto_detect_skills is False
    assert config.session_retention_days == 30
