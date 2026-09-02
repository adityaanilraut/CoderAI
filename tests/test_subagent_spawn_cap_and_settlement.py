"""Tests for subagent spawn cap configurability, 26-agent fan-out, settlement notices, and result inspection."""

from __future__ import annotations

import asyncio
import pathlib
import pytest
from unittest.mock import MagicMock, patch

from coderai.core.agents import (
    get_task_supervisor,
    spawn_background_agent,
)
from coderai.core.orchestration import (
    DEFAULT_MAX_CONTINUABLE_AGENTS,
    DEFAULT_MAX_RUNNING_JOBS,
    resolve_max_continuable_agents,
    resolve_max_running_jobs,
    settlement_summary,
)
from coderai.core.subagent import SubAgentManager, SubAgentResult, SubAgentSpec
from coderai.core.tools.agents import handle_list_agents_tool
from coderai.core.tools.types import ToolExecutionContext


def test_orchestration_defaults_and_env_overrides(monkeypatch: pytest.MonkeyPatch):
    """Verify resolve_max_continuable_agents and resolve_max_running_jobs resolve properly."""
    monkeypatch.delenv("CODERAI_MAX_CONTINUABLE_AGENTS_PER_SESSION", raising=False)
    monkeypatch.delenv("MAX_CONTINUABLE_AGENTS_PER_SESSION", raising=False)
    monkeypatch.delenv("CODERAI_MAX_RUNNING_JOBS_PER_SESSION", raising=False)
    monkeypatch.delenv("MAX_RUNNING_JOBS_PER_SESSION", raising=False)

    assert resolve_max_continuable_agents() == DEFAULT_MAX_CONTINUABLE_AGENTS
    assert resolve_max_running_jobs() == DEFAULT_MAX_RUNNING_JOBS

    # Test settings override
    assert resolve_max_continuable_agents({"orchestration": {"maxContinuableAgents": 75}}) == 75
    assert resolve_max_running_jobs({"orchestration": {"maxRunningJobs": 60}}) == 60

    # Test env override
    monkeypatch.setenv("CODERAI_MAX_CONTINUABLE_AGENTS_PER_SESSION", "35")
    assert resolve_max_continuable_agents() == 35

    monkeypatch.setenv("CODERAI_MAX_RUNNING_JOBS_PER_SESSION", "45")
    assert resolve_max_running_jobs() == 45


def test_settlement_summary_with_outcome():
    """Verify settlement_summary includes child outcome and report when provided."""
    # Basic without outcome
    summary1 = settlement_summary("agent_123", "completed")
    assert "Background subagent agent_123 finished" in summary1
    assert "Outcome:" not in summary1

    # With outcome / report
    summary2 = settlement_summary("agent_123", "completed", outcome="Found Aardvark and Acacia.")
    assert "Background subagent agent_123 finished" in summary2
    assert "Outcome:\nFound Aardvark and Acacia." in summary2


@pytest.mark.asyncio
async def test_spawn_26_subagents_a_to_z(tmp_path: pathlib.Path):
    """Verify spawning 26 subagents (A-Z) concurrently succeeds without hitting a 10-agent cap."""
    session_id = "test_session_a_to_z"

    # Create dummy manager with mock client
    mock_client = MagicMock()
    manager = SubAgentManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: mock_client,
    )

    # Mock manager.run_continuable so background tasks complete cleanly
    async def mock_run_continuable(spec: SubAgentSpec, sid: str) -> SubAgentResult:
        letter = spec.description.split()[-1]
        return SubAgentResult(
            task_id=spec.task_id,
            session_id=sid,
            status="completed",
            summary=f"Found Animal_{letter} and Plant_{letter}",
        )

    with patch.object(manager, "run_continuable", side_effect=mock_run_continuable):
        handles = []
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            spec = SubAgentSpec(
                description=f"Find {letter} names",
                prompt=f"Find animal and plant starting with {letter}",
                parent_session_id=session_id,
            )
            handle = await spawn_background_agent(manager, spec)
            handles.append(handle)

        assert len(handles) == 26

        # Wait for all 26 tasks to finish
        await asyncio.gather(*(h.task for h in handles if h.task))

        for h in handles:
            assert h.status == "completed"
            assert h.result is not None
            assert "Found Animal_" in h.result.summary


@pytest.mark.asyncio
async def test_list_agents_displays_results_and_supervisor(tmp_path: pathlib.Path):
    """Verify list_agents tool and TaskSupervisor return the result summary when completed."""
    session_id = "test_session_list_results"
    context = ToolExecutionContext(
        session_id=session_id,
        project_root=str(tmp_path),
        create_openai_client=lambda: MagicMock(),
    )

    manager = SubAgentManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: MagicMock(),
    )

    async def mock_run_continuable(spec: SubAgentSpec, sid: str) -> SubAgentResult:
        return SubAgentResult(
            task_id=spec.task_id,
            session_id=sid,
            status="completed",
            summary="Found Alligator and Aloe Vera.",
        )

    with patch.object(manager, "run_continuable", side_effect=mock_run_continuable):
        spec = SubAgentSpec(
            description="Find A names",
            prompt="Find animal and plant starting with A",
            parent_session_id=session_id,
        )
        handle = await spawn_background_agent(manager, spec)
        if handle.task:
            await handle.task

        # Test handle_list_agents_tool
        result = await handle_list_agents_tool({}, context)
        assert result.ok is True
        assert "Result: Found Alligator and Aloe Vera." in result.output

        # Test to_public_dict
        pub = handle.to_public_dict()
        assert pub["summary"] == "Found Alligator and Aloe Vera."

        # Test TaskSupervisor
        supervisor = get_task_supervisor()
        task_info = supervisor.get_task(handle.id)
        assert task_info is not None
        assert task_info["summary"] == "Found Alligator and Aloe Vera."


@pytest.mark.asyncio
async def test_spawn_cap_enforced_when_exceeded(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify that spawn cap raises descriptive error when limit is actually exceeded."""
    monkeypatch.setenv("CODERAI_MAX_CONTINUABLE_AGENTS_PER_SESSION", "3")

    session_id = "test_session_cap_enforcement"
    manager = SubAgentManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: MagicMock(),
    )

    # Keep tasks running
    block_event = asyncio.Event()

    async def mock_run_blocking(spec: SubAgentSpec, sid: str) -> SubAgentResult:
        await block_event.wait()
        return SubAgentResult(task_id=spec.task_id, session_id=sid, status="completed")

    with patch.object(manager, "run_continuable", side_effect=mock_run_blocking):
        handles = []
        for i in range(3):
            spec = SubAgentSpec(
                description=f"Agent {i}",
                prompt=f"Do work {i}",
                parent_session_id=session_id,
            )
            handle = await spawn_background_agent(manager, spec)
            handles.append(handle)

        # 4th agent should exceed cap of 3
        spec4 = SubAgentSpec(
            description="Agent 4",
            prompt="Do work 4",
            parent_session_id=session_id,
        )
        with pytest.raises(RuntimeError) as exc_info:
            await spawn_background_agent(manager, spec4)

        assert "subagent spawn cap reached: at most 3 live sub-agents" in str(exc_info.value)

        # Release the running tasks
        block_event.set()
        await asyncio.gather(*(h.task for h in handles if h.task))
