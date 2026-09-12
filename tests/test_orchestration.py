"""Consolidated orchestration tests: workflows, Ralph, goals, teams, jobs, guards.

Covers workflow caps/cancel, Ralph rounds/budget, goal/DSH lifecycle, team
board/messaging, jobs, git-history hermetic env, and concurrency guards. All
LLM/network access is mocked.
"""

from __future__ import annotations

import asyncio
import pathlib
import time
from types import SimpleNamespace as NS

import pytest

from coderai.utils.common.file_history import GitFileHistory
from coderai.subagents.builder import (
    check_subagent_depth_quota,
    cleanup_subagent_scratchpad,
    parse_subagent_descriptor,
    setup_subagent_scratchpad,
)
from coderai.teams import (
    TeamTaskBoard,
    get_team_manager,
    handle_spawn_teammate_tool,
    handle_team_task_create_tool,
    handle_team_task_get_tool,
    handle_team_task_list_tool,
    handle_team_task_update_tool,
    handle_wait_agent_tool,
    reset_team_manager,
)
from coderai.teams.deadlock import (
    CycleDetectedError,
    DeadlockError,
    InterAgentWaitWatchdog,
    assert_acyclic_dependencies,
    detect_task_cycles,
)
from coderai.tools.background import handle_job_kill_tool, handle_job_output_tool
from coderai.tools.legacy.path_lock import PathLockManager
from coderai.tools.agent import handle_subagent_tool
from coderai.tools.legacy.types import ToolExecutionContext


def _client_factory(content: str = "All done.", delay: float = 0.0):
    """Build a mock OpenAI client factory returning canned text content."""

    def _factory():
        def create(**kwargs):
            if delay:
                time.sleep(delay)
            return {
                "choices": [
                    {
                        "message": {
                            "content": content,
                            "tool_calls": None,
                            "reasoning_content": None,
                            "refusal": None,
                        }
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
            }

        return {
            "client": NS(chat=NS(completions=NS(create=create))),
            "model": "gpt-5.6-luna",
            "thinkingEnabled": False,
        }

    return _factory


def _tool_context(tmp_path, content: str = "All done."):
    """Build a ToolExecutionContext backed by the canned mock client."""
    return ToolExecutionContext(
        session_id="orch_session",
        project_root=str(tmp_path),
        create_openai_client=_client_factory(content),
    )


async def test_orchestration_background_job_completes(tmp_path: pathlib.Path):
    """Background Task tool creates a job that completes and kills as finished."""
    ctx = _tool_context(tmp_path)
    res = await handle_subagent_tool(
        {"description": "bg job", "prompt": "Inspect", "run_in_background": True}, ctx
    )
    assert res.ok is True and res.metadata["jobId"].startswith("subagent-")
    out = await handle_job_output_tool(
        {"job_id": res.metadata["jobId"], "wait": True, "timeout_ms": 5000}, ctx
    )
    assert out.ok is True and out.metadata["job"]["status"] == "completed"
    kill = await handle_job_kill_tool({"job_id": res.metadata["jobId"]}, ctx)
    assert kill.ok is True and kill.metadata["outcome"] == "already-finished"


def test_orchestration_team_board_gates_dependencies():
    """Task board blocks dependent tasks until their dependencies complete."""
    board = TeamTaskBoard()
    t1 = board.create_task(title="Design Schema", description="Design tables", priority="high")
    t2 = board.create_task(
        title="Migrate", description="Write migration", dependencies=[t1.task_id]
    )
    assert board.can_start_task(t1.task_id) is True
    assert board.can_start_task(t2.task_id) is False
    board.update_task(t1.task_id, status="completed", result="schema.sql created")
    assert board.can_start_task(t2.task_id) is True
    assert len(board.list_tasks()) == 2
    assert [t.task_id for t in board.list_tasks(status="pending")] == [t2.task_id]


def test_orchestration_team_messaging_direct_and_broadcast():
    """Teammates receive direct messages and broadcasts fan out to all."""
    reset_team_manager()
    mgr = get_team_manager()
    alice = mgr.spawn_teammate(name="Alice", role="architect")
    bob = mgr.spawn_teammate(name="Bob", role="coder")
    assert mgr.get_teammate("bob").teammate_id == bob.teammate_id
    msg = mgr.send_message(
        sender="Alice", recipient="Bob", content="Please implement the user model."
    )
    assert msg.recipient == "Bob" and bob.inbox[0].content == "Please implement the user model."
    assert len(alice.outbox) == 1
    mgr.send_message(sender="Alice", recipient="all", content="Standup in 5.")
    assert len(alice.inbox) == 1 and len(bob.inbox) == 2


async def test_orchestration_team_tools_manage_tasks(tmp_path: pathlib.Path):
    """Team tools spawn teammates and drive task create/get/update/list/wait."""
    reset_team_manager()
    ctx = _tool_context(tmp_path)
    spawn_res = await handle_spawn_teammate_tool(
        {"name": "Carol", "role": "reviewer", "mode": "read_only"}, ctx
    )
    assert spawn_res.ok is True and "Carol" in spawn_res.output
    carol_id = spawn_res.metadata["teammate_id"]
    task_res = await handle_team_task_create_tool(
        {
            "title": "Review PR #42",
            "description": "Check security",
            "assigned_to": carol_id,
            "priority": "high",
        },
        ctx,
    )
    assert task_res.ok is True
    task_id = task_res.metadata["task_id"]
    assert "Review PR #42" in (await handle_team_task_get_tool({"task_id": task_id}, ctx)).output
    update_res = await handle_team_task_update_tool(
        {"task_id": task_id, "status": "completed", "result": "Approved."}, ctx
    )
    assert update_res.ok is True and update_res.metadata["status"] == "completed"
    list_res = await handle_team_task_list_tool({"status": "completed"}, ctx)
    assert list_res.ok is True and len(list_res.metadata["tasks"]) == 1
    get_team_manager().get_teammate(carol_id).status = "completed"
    wait_res = await handle_wait_agent_tool({"agent_id": carol_id, "timeout_seconds": 2.0}, ctx)
    assert wait_res.ok is True and wait_res.metadata["status"] == "settled"


def test_orchestration_git_history_uses_hermetic_env(tmp_path: pathlib.Path):
    """Git history isolates itself from host gitconfig and initializes sessions."""
    history = GitFileHistory(str(tmp_path), str(tmp_path / ".git_history"))
    env = history._get_git_env()
    assert env.get("GIT_CONFIG_NOSYSTEM") == "1"
    assert env.get("GIT_CONFIG_GLOBAL") == "/dev/null"
    head = history.ensure_session("hermetic_test_session")
    assert head is not None and len(head) == 40


def test_orchestration_descriptor_parses_modes():
    """Subagent descriptors parse one-shot/continuable modes with tool filters."""
    desc = parse_subagent_descriptor(
        {"version": 1, "mode": "one-shot", "provider": "in_process", "label": "Research task"}
    )
    assert (desc.version, desc.mode, desc.provider) == (1, "one-shot", "in_process")
    desc_c = parse_subagent_descriptor(
        {
            "version": 1,
            "mode": "continuable",
            "provider": "in_process",
            "label": "Coder",
            "agentProvider": "deepseek",
            "agentModel": "deepseek-chat",
            "persona": "Expert Backend Developer",
            "toolFilter": {"allow": ["read", "grep", "write"], "deny": ["AskUserQuestion"]},
        }
    )
    assert desc_c.agent_provider == "deepseek" and desc_c.persona == "Expert Backend Developer"
    assert desc_c.tool_filter.is_tool_permitted("read") is True
    assert desc_c.tool_filter.is_tool_permitted("AskUserQuestion") is False
    assert desc_c.tool_filter.is_tool_permitted("unknown_tool") is False
    assert desc_c.to_dict()["mode"] == "continuable"


def test_orchestration_depth_quota_guards_scratchpad(tmp_path: pathlib.Path):
    """Depth quota rejects overflow and scratchpads set up/clean up cleanly."""
    import os

    ok, err = check_subagent_depth_quota(current_depth=2, max_depth=3)
    assert ok is True and err is None
    ok, err = check_subagent_depth_quota(current_depth=3, max_depth=3)
    assert ok is False and "RecursionLimitError" in (err or "")
    sp_path = setup_subagent_scratchpad(str(tmp_path), "test_session_123")
    assert os.path.exists(sp_path) and "test_session_123" in sp_path
    cleanup_subagent_scratchpad(sp_path)


def test_orchestration_cycle_detection_rejects_loops():
    """Task-graph cycle detection flags direct and indirect dependency loops."""
    assert detect_task_cycles({"A": ["B"], "B": ["C"], "C": []}) is None
    assert_acyclic_dependencies({"A": ["B"], "B": ["C"], "C": []})
    for graph in ({"A": ["B"], "B": ["A"]}, {"A": ["B"], "B": ["C"], "C": ["A"]}):
        cycle = detect_task_cycles(graph)
        assert cycle is not None and cycle[0] == cycle[-1]
        with pytest.raises(CycleDetectedError):
            assert_acyclic_dependencies(graph)


def test_orchestration_wait_watchdog_detects_deadlock():
    """Inter-agent wait watchdog raises once a wait cycle closes."""
    watchdog = InterAgentWaitWatchdog()
    watchdog.record_wait("agent_A", "agent_B")
    watchdog.record_wait("agent_B", "agent_C")
    with pytest.raises(DeadlockError, match="DeadlockError"):
        watchdog.record_wait("agent_C", "agent_A")


async def test_orchestration_path_locks_isolate_writes():
    """Path locks serialize same-path writers while disjoint paths run parallel."""
    lock_mgr = PathLockManager()
    active_readers, max_readers, writer_active = 0, 0, False

    async def _reader():
        nonlocal active_readers, max_readers, writer_active
        async with lock_mgr.acquire_read_lock("/workspace/file_a.py"):
            assert not writer_active
            active_readers += 1
            max_readers = max(max_readers, active_readers)
            await asyncio.sleep(0.05)
            active_readers -= 1

    async def _writer(path):
        nonlocal writer_active, active_readers
        async with lock_mgr.acquire_write_lock(path):
            assert active_readers == 0
            writer_active = True
            await asyncio.sleep(0.05)
            writer_active = False

    await asyncio.gather(*[_reader() for _ in range(4)])
    assert max_readers > 1
    await asyncio.gather(_reader(), _writer("/workspace/file_a.py"), _reader())
    t0 = time.time()
    await asyncio.gather(_writer("/workspace/file_a.py"), _writer("/workspace/file_b.py"))
    assert time.time() - t0 < 0.12


async def test_autonomous_teammate_execution_and_settlement(monkeypatch):
    """Verify autonomous teammate worker executes assigned in_progress tasks and wait_agent settles.

    The sub-agent run itself is stubbed: this test covers worker mechanics
    (pickup → completed → settlement), not LLM execution.
    """
    reset_team_manager()
    mgr = get_team_manager()
    # Spawn teammate with discovered role
    tm = mgr.spawn_teammate(name="Dan", role="architect")
    assert tm.role == "architect"
    assert tm.system_prompt is not None  # Discovered from .coderai/agents/architect.md

    async def _fake_execute(teammate, task):
        assert task.title == "Design Cache Schema"
        return f"Report for '{task.title}' by {teammate.name}"

    monkeypatch.setattr(mgr, "_execute_task", _fake_execute)

    # Create task assigned to Dan
    task = mgr.task_board.create_task(
        title="Design Cache Schema",
        description="Write high-level architecture for caching",
        assigned_to=tm.teammate_id,
        priority="high",
    )
    assert task.status == "pending"

    # Kick off task execution by setting status to in_progress
    mgr.task_board.update_task(task.task_id, status="in_progress")

    # Await settlement via wait_agent
    settle_res = await mgr.wait_agent(tm.teammate_id, timeout_seconds=5.0)
    assert settle_res["ok"] is True
    assert settle_res["status"] == "settled"

    updated_task = mgr.task_board.get_task(task.task_id)
    assert updated_task.status == "completed"
    assert "Design Cache Schema" in (updated_task.result or "")
    assert tm.status == "completed"
    assert "Design Cache Schema" in (tm.last_report or "")
    mgr.cancel_all_teammates()


async def test_autonomous_teammate_failure_marks_failed(monkeypatch):
    """A sub-agent error must mark the task `failed` — never `completed`."""
    reset_team_manager()
    mgr = get_team_manager()
    tm = mgr.spawn_teammate(name="Erin", role="coder")

    async def _boom(teammate, task):
        raise RuntimeError("AuthenticationError: Missing API client.")

    monkeypatch.setattr(mgr, "_execute_task", _boom)

    task = mgr.task_board.create_task(
        title="Broken Task",
        description="This execution always fails",
        assigned_to=tm.teammate_id,
    )
    mgr.task_board.update_task(task.task_id, status="in_progress")

    settle_res = await mgr.wait_agent(tm.teammate_id, timeout_seconds=5.0)
    assert settle_res["ok"] is True
    assert settle_res["status"] == "settled"

    updated_task = mgr.task_board.get_task(task.task_id)
    assert updated_task.status == "failed"
    assert "Missing API client" in (updated_task.result or "")
    assert tm.status == "failed"
    mgr.cancel_all_teammates()


async def test_execute_task_uses_subagent_manager(monkeypatch):
    """_execute_task drives SubAgentManager.spawn_subagent and surfaces failures."""
    from coderai.subagents.output import SubAgentResult

    reset_team_manager()
    mgr = get_team_manager()
    tm = mgr.spawn_teammate(name="Farah", role="coder")
    task = mgr.task_board.create_task(
        title="Real Path",
        description="exercises the SubAgentManager call path",
        assigned_to=tm.teammate_id,
    )

    seen: dict[str, object] = {}

    class _FakeRunner:
        def __init__(self, project_root, create_openai_client):
            seen["project_root"] = project_root

        async def spawn_subagent(self, spec):
            seen["spec"] = spec
            return SubAgentResult(
                task_id=spec.task_id,
                session_id="s1",
                status="completed",
                summary="Cache schema: LRU over Redis.",
            )

    monkeypatch.setattr("coderai.subagents.runner.SubAgentManager", _FakeRunner)
    report = await mgr._execute_task(tm, task)
    assert report == "Cache schema: LRU over Redis."
    assert seen["spec"].subagent_type == "coder"

    class _FailingRunner(_FakeRunner):
        async def spawn_subagent(self, spec):
            return SubAgentResult(
                task_id=spec.task_id,
                session_id="s1",
                status="failed",
                summary="",
                error="AuthenticationError: Missing API client.",
            )

    monkeypatch.setattr("coderai.subagents.runner.SubAgentManager", _FailingRunner)
    with pytest.raises(RuntimeError, match="Missing API client"):
        await mgr._execute_task(tm, task)
    mgr.cancel_all_teammates()
