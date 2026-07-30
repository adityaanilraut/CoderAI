"""Focused coverage for persisted accounting and provider replacement wiring."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coderAI.core.agent import Agent
from coderAI.core.execution_context import create_run_context
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
    agent.run_context = create_run_context(workspace_root=".")
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


def test_failed_resume_clears_previous_session_recovery_binding() -> None:
    session = Session(session_id="session_1_abcdef12")
    agent = _bare_agent(session)
    agent._bind_session_run_context(session)
    agent.active_plan_id = "plan-old"
    agent.active_plan_revision = 3
    agent._plan_execution_ready = True
    history = SimpleNamespace(load_session=MagicMock(return_value=None))

    with patch(
        "coderAI.core.agent_session.get_services",
        return_value=SimpleNamespace(history=history),
    ):
        loaded = agent.load_session("session_2_abcdef12")

    assert loaded is None
    assert agent.run_context.session_id is None
    assert agent.run_context.checkpoint_store is None
    assert agent.run_context.transaction_store is None
    assert agent.active_plan_id is None
    assert agent.active_plan_revision is None
    assert agent._plan_execution_ready is False


def test_load_session_recovers_open_workspace_transaction(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "target.txt"
    target.write_text("before")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    session = Session(session_id="session_1_txresume12")

    original = _bare_agent(session)
    original.config = SimpleNamespace(project_root=str(workspace), save_history=True)
    original.run_context = create_run_context(workspace_root=str(workspace))
    original._bind_session_run_context(session)
    store = original.run_context.transaction_store
    handle = store.begin(
        run_context=original.run_context,
        tool_call_id="call_resume",
        tool_name="write_file",
        tool_arguments={"path": "target.txt"},
        objective="resume recovery",
        plan_id=None,
        plan_revision=None,
    )
    target.write_text("after crash")

    resumed = _bare_agent(session)
    resumed.config = SimpleNamespace(project_root=str(workspace), save_history=True)
    resumed.run_context = create_run_context(workspace_root=str(workspace))
    history = SimpleNamespace(
        load_session=MagicMock(return_value=session),
        save_session_data=MagicMock(),
    )
    with patch(
        "coderAI.core.agent_session.get_services",
        return_value=SimpleNamespace(history=history),
    ):
        loaded = resumed.load_session(session.session_id)

    assert loaded is session
    meta = session.metadata["workspace_transactions"]
    assert meta["recovered_transactions"] == [handle.transaction_id]
    assert resumed.run_context.transaction_store.list_transactions()[0]["state"] == "recovered"
    history.save_session_data.assert_called_once()


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


def test_context_usage_reports_model_limit_instead_of_config_fallback() -> None:
    agent = Agent.__new__(Agent)
    agent.session = None
    agent._context_controller = SimpleNamespace(
        inject_context=lambda messages: messages,
        estimate_tokens=lambda messages: 3,
        _effective_context_limit=lambda override: 1_000_000,
    )

    assert agent.get_context_usage() == (3, 1_000_000)
