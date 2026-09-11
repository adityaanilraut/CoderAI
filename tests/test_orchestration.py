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

from coderai.core.common.file_history import GitFileHistory
from coderai.core.goals_dsh import (
    BLOCK_CODE_ROUND_LIMIT,
    DSHGoalStore,
    GoalBlockReason,
    GoalError,
    get_dsh_goal_store,
    reset_dsh_goal_store,
)
from coderai.core.goal_round_driver import (
    finish_goal_round,
    maybe_queue_goal_round,
)
from coderai.core.orchestration import WorkflowLimits
from coderai.core.spawn import (
    check_subagent_depth_quota,
    cleanup_subagent_scratchpad,
    parse_subagent_descriptor,
    setup_subagent_scratchpad,
)
from coderai.core.teams import (
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
from coderai.core.teams.deadlock import (
    CycleDetectedError,
    DeadlockError,
    InterAgentWaitWatchdog,
    assert_acyclic_dependencies,
    detect_task_cycles,
)
from coderai.core.tools.goal_dsh import (
    handle_create_goal_tool,
    handle_get_goal_tool,
    handle_update_goal_tool,
)
from coderai.core.tools.jobs import handle_job_kill_tool, handle_job_output_tool
from coderai.core.tools.path_lock import PathLockManager
from coderai.core.tools.ralph import RalphHandoff, _validate_report, handle_ralph_tool
from coderai.core.tools.subagent import handle_subagent_tool
from coderai.core.tools.types import ToolExecutionContext
from coderai.core.workflow.engine import WorkflowContext, execute_workflow_script


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


@pytest.fixture
def goal_store(tmp_path: pathlib.Path):
    """Provide an isolated DSH goal store, reset before and after."""
    reset_dsh_goal_store()
    yield get_dsh_goal_store(str(tmp_path))
    reset_dsh_goal_store()


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


async def test_orchestration_workflow_runs_pipeline_parallel(tmp_path: pathlib.Path):
    """Workflow harness supports pipeline, parallel, phase, log, and args."""
    ctx = WorkflowContext(
        workflow_id="wf_contract",
        name="contract",
        project_root=str(tmp_path),
        create_openai_client=None,
    )
    result = await execute_workflow_script(
        """
pipeline_out = await pipeline([1, 2, 3], lambda prev, item, index: prev + index,
                              lambda prev, item, index: f"v_{prev}")
parallel_out = await parallel([lambda: "ok", lambda: 1 / 0])
phase("agg")
log("aggregating")
return {"pipeline": pipeline_out, "parallel": parallel_out, "n": len(args.get("files", []))}
""",
        {"files": [1, 2]},
        ctx,
    )
    assert result.status == "completed" and result.stop_reason == "completed"
    assert result.output == {"pipeline": ["v_1", "v_3", "v_5"], "parallel": ["ok", None], "n": 2}


async def test_orchestration_workflow_enforces_agent_cap(tmp_path: pathlib.Path):
    """Workflow fails once total spawned agents exceed the configured cap."""
    ctx = WorkflowContext(
        workflow_id="wf_cap",
        name="cap",
        project_root=str(tmp_path),
        create_openai_client=_client_factory(),
        limits=WorkflowLimits(max_concurrent_agents=2, max_total_agents=2, max_items_per_call=4096),
    )
    result = await execute_workflow_script(
        'a = await agent("one")\nb = await agent("two")\n'
        'c = await agent("three")\nreturn [a, b, c]',
        None,
        ctx,
    )
    assert result.status == "failed" and "total agent cap (2)" in str(result.error)


async def test_orchestration_workflow_enforces_item_cap(tmp_path: pathlib.Path):
    """Workflow fails when a single parallel call exceeds the item cap."""
    ctx = WorkflowContext(workflow_id="wf_items", name="items", project_root=str(tmp_path))
    result = await execute_workflow_script("await parallel([lambda: 1] * 5000)", None, ctx)
    assert result.status == "failed" and "per-call cap" in str(result.error)


async def test_orchestration_workflow_cancel_stops_children(tmp_path: pathlib.Path):
    """Pre-cancelled workflow settles cancelled and wins over settled returns."""
    ctx = WorkflowContext(workflow_id="wf_cancel", name="cancel", project_root=str(tmp_path))
    ctx.cancel("parent step aborted")
    result = await execute_workflow_script("return 1", None, ctx)
    assert result.status == "cancelled" and result.stop_reason == "cancelled"
    ctx2 = WorkflowContext(workflow_id="wf_cancel2", name="cancel2", project_root=str(tmp_path))
    ctx2.cancel("late")
    assert (await execute_workflow_script("log('x')\nreturn 2", None, ctx2)).status == "cancelled"


async def test_orchestration_workflow_slot_waiter_rejects_on_cancel(tmp_path: pathlib.Path):
    """Cancelling a slot-contended run settles the workflow without hanging."""
    ctx = WorkflowContext(
        workflow_id="wf_slots",
        name="slots",
        project_root=str(tmp_path),
        create_openai_client=_client_factory("done", delay=0.2),
        limits=WorkflowLimits(
            max_concurrent_agents=1, max_total_agents=10, max_items_per_call=4096
        ),
    )
    task = asyncio.create_task(
        execute_workflow_script(
            'a = await parallel([lambda: agent("first"), lambda: agent("second")])\nreturn a',
            None,
            ctx,
        )
    )
    await asyncio.sleep(0.05)
    ctx.cancel("parent step aborted")
    await asyncio.wait_for(task, timeout=10)


def test_orchestration_ralph_validates_reports():
    """Ralph handoff validation enforces per-status required fields."""
    assert (
        _validate_report(RalphHandoff(status="continue", summary="w", next_steps="run tests"))
        is None
    )
    assert _validate_report(RalphHandoff(status="continue", summary="w", next_steps="")) is not None
    assert (
        _validate_report(RalphHandoff(status="complete", summary="d", evidence="tests pass"))
        is None
    )
    assert (
        _validate_report(RalphHandoff(status="complete", summary="d", next_steps="more"))
        is not None
    )
    assert (
        _validate_report(RalphHandoff(status="blocked", summary="s", blocker="no docker")) is None
    )
    assert _validate_report(RalphHandoff(status="blocked", summary="s")) is not None


async def test_orchestration_ralph_budget_marks_round_failed(tmp_path: pathlib.Path):
    """Non-JSON model output fails the round instead of looping forever."""
    res = await handle_ralph_tool(
        {"objective": "Verify budget behavior", "max_rounds": 1}, _tool_context(tmp_path)
    )
    assert res.ok is False and res.metadata["status"] == "round-failed"


async def test_orchestration_ralph_rejects_excess_rounds(tmp_path: pathlib.Path):
    """Ralph rejects max_rounds above the ceiling."""
    res = await handle_ralph_tool(
        {"objective": "Verify ceiling", "max_rounds": 999_999}, _tool_context(tmp_path)
    )
    assert res.ok is False and "ceiling" in (res.error or "")


def test_orchestration_goal_lifecycle_enforces_cas(goal_store: DSHGoalStore):
    """Goal pause/resume/complete enforce CAS revisions; block carries a reason code."""
    goal = goal_store.create("sess", "Ship the feature", max_goal_rounds=5)
    assert (goal.phase, goal.activation, goal.max_goal_rounds) == ("active", "armed", 5)
    with pytest.raises(GoalError):
        goal_store.complete("sess", goal.ref().__class__(id=goal.id, revision=999))
    assert goal_store.pause("sess", goal.ref()).phase == "paused"
    resumed = goal_store.resume("sess", goal_store.get("sess").ref())
    assert resumed.phase == "active"
    assert goal_store.complete("sess", resumed.ref()).phase == "complete"
    goal2 = goal_store.create("sess2", "Other goal")
    blocked = goal_store.block(
        "sess2", goal2.ref(), GoalBlockReason(code="model-reported", message="needs human")
    )
    assert blocked.phase == "blocked" and blocked.blocked_reason.code == "model-reported"


def test_orchestration_goal_reload_disarms_activation(
    goal_store: DSHGoalStore, tmp_path: pathlib.Path
):
    """Reloaded goals start disarmed so they never auto-run after restart."""
    goal_store.create("sess3", "Persisted goal")
    reset_dsh_goal_store()
    reloaded = get_dsh_goal_store(str(tmp_path)).get("sess3")
    assert reloaded is not None and reloaded.activation == "disarmed"


async def test_orchestration_goal_tools_reject_stale_and_subagent(
    tmp_path: pathlib.Path, goal_store: DSHGoalStore
):
    """Goal tools reject stale revisions and subagent callers lacking authority."""
    ctx = ToolExecutionContext(session_id="sess4", project_root=str(tmp_path))
    created = await handle_create_goal_tool({"objective": "Ship it", "max_goal_rounds": 4}, ctx)
    assert created.ok is True
    goal_id, revision = created.metadata["goal"]["id"], created.metadata["goal"]["revision"]
    assert (await handle_get_goal_tool({}, ctx)).metadata["goal"]["id"] == goal_id
    stale = await handle_update_goal_tool(
        {"goal_id": goal_id, "revision": revision + 1, "action": "complete"}, ctx
    )
    assert stale.ok is False and "STALE" in stale.error
    done = await handle_update_goal_tool(
        {"goal_id": goal_id, "revision": revision, "action": "complete"}, ctx
    )
    assert done.ok is True and done.metadata["goal"]["phase"] == "complete"
    sub_ctx = ToolExecutionContext(session_id="sub_parent_ab_task1", project_root=str(tmp_path))
    rejected = await handle_create_goal_tool({"objective": "Nested"}, sub_ctx)
    assert rejected.ok is False and "authority" in (rejected.error or "").lower()


async def test_orchestration_goal_block_threshold_applies_in_round(
    tmp_path: pathlib.Path, goal_store: DSHGoalStore
):
    """Model-reported blocked is deferred while an automatic round is under threshold."""
    ctx = ToolExecutionContext(session_id="sess5", project_root=str(tmp_path))
    first = await handle_create_goal_tool({"objective": "Long goal"}, ctx)
    blocked_now = await handle_update_goal_tool(
        {
            "goal_id": first.metadata["goal"]["id"],
            "revision": first.metadata["goal"]["revision"],
            "action": "blocked",
            "blocked_reason": "stuck",
        },
        ctx,
    )
    assert blocked_now.ok is True
    second = await handle_create_goal_tool({"objective": "Long goal 2"}, ctx)
    goal_store.set_in_goal_round("sess5", True)
    try:
        early = await handle_update_goal_tool(
            {
                "goal_id": second.metadata["goal"]["id"],
                "revision": second.metadata["goal"]["revision"],
                "action": "blocked",
                "blocked_reason": "stuck",
            },
            ctx,
        )
        assert early.ok is False and "BLOCK_THRESHOLD" in early.error
    finally:
        goal_store.set_in_goal_round("sess5", False)


def test_orchestration_goal_round_queues_until_limit(
    tmp_path: pathlib.Path, goal_store: DSHGoalStore
):
    """Goal rounds queue until the round cap blocks the goal with a limit code."""
    appended: list = []

    class StubManager:
        project_root = str(tmp_path)

        def get_resolved_settings(self):
            return {}

        def _append_message(self, message):
            appended.append((message.session_id, message.content, message.meta or {}))

        def _build_message(self, session_id, role, content, **kwargs):
            return NS(session_id=session_id, role=role, content=content, meta=kwargs.get("meta"))

    goal_store.create("sess6", "Drive to completion", max_goal_rounds=2)
    assert maybe_queue_goal_round(StubManager(), "sess6") is True
    assert "<goal_round>" in appended[0][1] and goal_store.in_goal_round("sess6") is True
    finish_goal_round("sess6", str(tmp_path), entry_status="completed")
    assert maybe_queue_goal_round(StubManager(), "sess6") is True
    finish_goal_round("sess6", str(tmp_path), entry_status="completed")
    assert maybe_queue_goal_round(StubManager(), "sess6") is False
    blocked = goal_store.get("sess6")
    assert blocked.phase == "blocked" and blocked.blocked_reason.code == BLOCK_CODE_ROUND_LIMIT
    goal_store.create("sess7", "Another")
    goal_store.disarm("sess7")
    assert maybe_queue_goal_round(StubManager(), "sess7") is False


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


async def test_autonomous_teammate_execution_and_settlement():
    """Verify autonomous teammate worker executes assigned in_progress tasks and wait_agent settles."""
    reset_team_manager()
    mgr = get_team_manager()
    # Spawn teammate with discovered role
    tm = mgr.spawn_teammate(name="Dan", role="architect")
    assert tm.role == "architect"
    assert tm.system_prompt is not None  # Discovered from .coderai/agents/architect.md

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

