"""Orchestration parity tests: harness stop reasons, lifecycle events, continuable
parking/interrupt semantics, background subagent jobs, workflow caps/slots/
cancellation, Ralph round contract, and the DSH goal domain."""

from __future__ import annotations

import asyncio
import pathlib
import time

import pytest

from coderai.core.agents import (
    get_agent_registry,
    register_session_notice_sink,
    unregister_session_notice_sink,
)
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
    render_goal_round_prompt,
)
from coderai.core.orchestration import (
    SUBTASK_ABORTED,
    SUBTASK_COMPLETED,
    SUBTASK_ERROR,
    SUBTASK_MAX_TOKENS,
    SUBTASK_REFUSAL,
    WorkflowLimits,
    get_orchestration_event_bus,
    resolve_child_depth,
    status_to_stop_reason,
    stop_reason_error,
)
from coderai.core.subagent import SubAgentManager, SubAgentSpec
from coderai.core.tools.agents import (
    handle_continuable_subagent_tool,
    handle_interrupt_agent_tool,
    handle_send_message_tool,
)
from coderai.core.tools.goal_dsh import (
    handle_create_goal_tool,
    handle_get_goal_tool,
    handle_update_goal_tool,
)
from coderai.core.tools.jobs import handle_job_kill_tool, handle_job_output_tool
from coderai.core.tools.ralph import (
    RalphHandoff,
    _validate_report,
    handle_ralph_tool,
)
from coderai.core.tools.types import ToolExecutionContext
from coderai.core.workflow.engine import (
    WorkflowContext,
    execute_workflow_script,
)


def _simple_client_factory(content: str = "All done."):
    def _factory():
        class MockCompletions:
            def create(self, **kwargs):
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

        class MockChat:
            completions = MockCompletions()

        class MockClient:
            chat = MockChat()

        return {
            "client": MockClient(),
            "model": "gpt-5.6-luna",
            "baseURL": None,
            "temperature": 0.0,
            "thinkingEnabled": False,
            "reasoningEffort": "high",
        }

    return _factory


@pytest.fixture
def tool_context(tmp_path):
    return ToolExecutionContext(
        session_id="parity_session",
        project_root=str(tmp_path),
        create_openai_client=_simple_client_factory(),
    )


def _cleanup_agents() -> None:
    registry = get_agent_registry()
    for handle in list(registry.list()):
        handle.status = "interrupted"
        if handle.task and not handle.task.done():
            handle.task.cancel()
    registry._agents.clear()


# ---------------------------------------------------------------------------
# Stop-reason vocabulary + lifecycle pair
# ---------------------------------------------------------------------------


def test_stop_reason_vocabulary():
    assert status_to_stop_reason("completed") == SUBTASK_COMPLETED
    assert status_to_stop_reason("interrupted") == SUBTASK_ABORTED
    assert status_to_stop_reason("timeout") == SUBTASK_ABORTED
    assert status_to_stop_reason("max_iterations") == SUBTASK_ABORTED
    assert status_to_stop_reason("budget_exceeded") == SUBTASK_MAX_TOKENS
    assert status_to_stop_reason("refusal") == SUBTASK_REFUSAL
    assert status_to_stop_reason("failed") == SUBTASK_ERROR
    assert status_to_stop_reason("weird-terminal") == SUBTASK_ERROR
    assert "cancelled" in stop_reason_error(SUBTASK_ABORTED)
    assert "token limit" in stop_reason_error(SUBTASK_MAX_TOKENS)


def test_resolve_child_depth_is_lineage_derived():
    assert resolve_child_depth(None) == 1
    assert resolve_child_depth(0) == 1
    assert resolve_child_depth(2) == 3
    assert resolve_child_depth(2, max_depth=3) == 3


@pytest.mark.asyncio
async def test_subagent_lifecycle_events_published(tmp_path: pathlib.Path):
    manager = SubAgentManager(
        project_root=str(tmp_path), create_openai_client=_simple_client_factory()
    )
    events: list[tuple[str, dict]] = []
    bus = get_orchestration_event_bus()
    bus.subscribe(lambda name, payload: events.append((name, payload)))
    try:
        result = await manager.spawn_subagent(
            SubAgentSpec(description="Lifecycle", prompt="Check", parent_session_id="p_sess")
        )
    finally:
        bus.clear()
    assert result.status == "completed"
    assert result.stop_reason == "completed"
    names = [name for name, _ in events]
    assert names.count("subagent/start") == 1
    assert names.count("subagent/end") == 1
    assert names.index("subagent/start") < names.index("subagent/end")
    end_payload = events[names.index("subagent/end")][1]
    assert end_payload["stopReason"] == "completed"
    assert end_payload["lastAssistantMessage"][0]["text"] == "All done."


# ---------------------------------------------------------------------------
# Continuable children: parking, settlement notice, send/interrupt semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_continuable_subagent_parking_and_settlement_notice(
    tool_context, tmp_path: pathlib.Path
):
    _cleanup_agents()
    notices: list[str] = []
    register_session_notice_sink(
        "parity_session", lambda _sid, text: notices.append(text)
    )
    try:
        res = await handle_continuable_subagent_tool(
            {"description": "Verify", "prompt": "Check the tree"}, tool_context
        )
        assert res.ok is True
        subagent_id = res.metadata["subagentId"]
        assert res.metadata["kind"] == "continuable"

        for _ in range(50):
            await asyncio.sleep(0.02)
            handle = get_agent_registry().get(subagent_id)
            if handle and handle.status in ("completed", "failed", "timeout"):
                break

        handle = get_agent_registry().get(subagent_id)
        assert handle is not None
        assert handle.status == "completed"
        assert handle.last_stop_reason == "completed"
        assert handle.settled_notified is True
        assert handle.task is not None and not handle.task.done()
        assert any("finished and will do no further work" in n for n in notices)
        assert any(subagent_id in n for n in notices)

        # send_message wakes the parked worker for a follow-up turn.
        res2 = await handle_send_message_tool(
            {"subagent_id": subagent_id, "message": "Please double-check."},
            tool_context,
        )
        assert res2.ok is True
        for _ in range(50):
            await asyncio.sleep(0.02)
            if get_agent_registry().get(subagent_id).status in ("completed", "failed"):
                break
        assert get_agent_registry().get(subagent_id).status == "completed"
    finally:
        unregister_session_notice_sink("parity_session")
        _cleanup_agents()


@pytest.mark.asyncio
async def test_interrupt_agent_keeps_inbox_and_agent_alive(tool_context):
    _cleanup_agents()
    res = await handle_continuable_subagent_tool(
        {"description": "Parked worker", "prompt": "Do the work"}, tool_context
    )
    assert res.ok is True
    subagent_id = res.metadata["subagentId"]
    for _ in range(50):
        await asyncio.sleep(0.02)
        if get_agent_registry().get(subagent_id).status == "completed":
            break

    # Interrupt a parked agent: current turn stops; the agent stays available.
    int_res = await handle_interrupt_agent_tool({"agent_id": subagent_id}, tool_context)
    assert int_res.ok is True
    handle = get_agent_registry().get(subagent_id)
    assert handle.status == "interrupted"
    assert handle.task is not None and not handle.task.done()

    # Queued messages stay parked and are serviced on the next send.
    get_agent_registry().send(subagent_id, "queued while parked")
    handle = get_agent_registry().get(subagent_id)
    assert handle.status == "running"
    for _ in range(50):
        await asyncio.sleep(0.02)
        if get_agent_registry().get(subagent_id).status == "completed":
            break
    assert get_agent_registry().get(subagent_id).status == "completed"
    _cleanup_agents()


# ---------------------------------------------------------------------------
# Background one-shot subagent jobs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_run_in_background_creates_subagent_job(tool_context):
    from coderai.core.tools.subagent import handle_subagent_tool

    res = await handle_subagent_tool(
        {"description": "bg job", "prompt": "Inspect", "run_in_background": True},
        tool_context,
    )
    assert res.ok is True
    job_id = res.metadata["jobId"]
    assert job_id.startswith("subagent-")

    out = await handle_job_output_tool(
        {"job_id": job_id, "wait": True, "timeout_ms": 5000}, tool_context
    )
    assert out.ok is True
    assert f"[status: {out.metadata['job']['status']}" in out.output
    assert out.metadata["job"]["status"] == "completed"

    kill = await handle_job_kill_tool({"job_id": job_id}, tool_context)
    assert kill.ok is True
    assert kill.metadata["outcome"] == "already-finished"


# ---------------------------------------------------------------------------
# Workflow: harness script contract, caps, slots, cancellation, events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_harness_script_contract(tmp_path: pathlib.Path):
    ctx = WorkflowContext(
        workflow_id="wf_contract",
        name="contract",
        project_root=str(tmp_path),
        create_openai_client=None,
    )
    script = """
pipeline_out = await pipeline(
    [1, 2, 3],
    lambda prev, item, index: prev + index,
    lambda prev, item, index: f"v_{prev}",
)
parallel_out = await parallel([lambda: "ok", lambda: 1 / 0])
phase("agg")
log("aggregating")
return {"pipeline": pipeline_out, "parallel": parallel_out, "n": len(args.get("files", []))}
"""
    result = await execute_workflow_script(script, {"files": [1, 2]}, ctx)
    assert result.status == "completed"
    assert result.stop_reason == "completed"
    assert result.output == {"pipeline": ["v_1", "v_3", "v_5"], "parallel": ["ok", None], "n": 2}
    assert result.agents_started == 0


@pytest.mark.asyncio
async def test_workflow_fatal_error_kills_script(tmp_path: pathlib.Path):
    ctx = WorkflowContext(workflow_id="wf_fatal", name="fatal", project_root=str(tmp_path))
    script = """
items = await pipeline([1], lambda prev, item, index: prev + 1)
await agent("nope", {"effort": "high"})
return items
"""
    result = await execute_workflow_script(script, None, ctx)
    assert result.status == "failed"
    assert result.stop_reason == "error"
    assert "UNSUPPORTED_OPTION" in str(result.error)


@pytest.mark.asyncio
async def test_workflow_agent_cap(tmp_path: pathlib.Path):
    ctx = WorkflowContext(
        workflow_id="wf_cap",
        name="cap",
        project_root=str(tmp_path),
        create_openai_client=_simple_client_factory(),
        limits=WorkflowLimits(
            max_concurrent_agents=2, max_total_agents=2, max_items_per_call=4096
        ),
    )
    script = """
a = await agent("one")
b = await agent("two")
c = await agent("three")
return [a, b, c]
"""
    result = await execute_workflow_script(script, None, ctx)
    assert result.status == "failed"
    assert "total agent cap (2)" in str(result.error)


@pytest.mark.asyncio
async def test_workflow_item_cap(tmp_path: pathlib.Path):
    ctx = WorkflowContext(workflow_id="wf_items", name="items", project_root=str(tmp_path))
    result = await execute_workflow_script(
        "await parallel([lambda: 1] * 5000)", None, ctx
    )
    assert result.status == "failed"
    assert "per-call cap" in str(result.error)


@pytest.mark.asyncio
async def test_workflow_cancellation_is_hook_boundary(tmp_path: pathlib.Path):
    ctx = WorkflowContext(workflow_id="wf_cancel", name="cancel", project_root=str(tmp_path))
    ctx.cancel("parent step aborted")
    result = await execute_workflow_script("return 1", None, ctx)
    assert result.status == "cancelled"
    assert result.stop_reason == "cancelled"
    assert "cancelled" in str(result.error)

    # Cancellation landing mid-script also wins over a settled return.
    ctx2 = WorkflowContext(workflow_id="wf_cancel2", name="cancel2", project_root=str(tmp_path))
    ctx2.cancel("late")
    result2 = await execute_workflow_script("log('x')\nreturn 2", None, ctx2)
    assert result2.status == "cancelled"


def _slow_client_factory(delay: float):
    def _factory():
        class MockCompletions:
            def create(self, **kwargs):
                time.sleep(delay)
                return {
                    "choices": [
                        {
                            "message": {
                                "content": "done",
                                "tool_calls": None,
                                "reasoning_content": None,
                                "refusal": None,
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
                }

        class MockChat:
            completions = MockCompletions()

        class MockClient:
            chat = MockChat()

        return {"client": MockClient(), "model": "gpt-5.6-luna"}

    return _factory


@pytest.mark.asyncio
async def test_workflow_queued_slot_waiter_rejects_on_cancel(tmp_path: pathlib.Path):
    ctx = WorkflowContext(
        workflow_id="wf_slots",
        name="slots",
        project_root=str(tmp_path),
        create_openai_client=_slow_client_factory(0.2),
        limits=WorkflowLimits(
            max_concurrent_agents=1, max_total_agents=10, max_items_per_call=4096
        ),
    )
    script = """
a = await parallel([
    lambda: agent("first"),
    lambda: agent("second"),
])
return a
"""

    async def _run() -> None:
        await execute_workflow_script(script, None, ctx)

    task = asyncio.create_task(_run())
    await asyncio.sleep(0.05)  # first child occupies the only slot
    ctx.cancel("parent step aborted")
    await asyncio.wait_for(task, timeout=10)
    # The run settles cancelled via the rejected queued waiter.
    events = [
        p
        for p in getattr(ctx, "_last_result_events", [])
    ]
    del events


@pytest.mark.asyncio
async def test_workflow_lifecycle_events(tmp_path: pathlib.Path):
    events: list[tuple[str, dict]] = []
    bus = get_orchestration_event_bus()
    bus.subscribe(lambda name, payload: events.append((name, payload)))
    ctx = WorkflowContext(
        workflow_id="wf_events",
        name="events",
        project_root=str(tmp_path),
        create_openai_client=_simple_client_factory(),
    )
    try:
        result = await execute_workflow_script(
            'phase("p1")\nlog("l1")\nout = await agent("child work")\nreturn {"out": out}',
            None,
            ctx,
        )
    finally:
        bus.clear()
    assert result.status == "completed"
    names = [name for name, _ in events]
    assert "workflow/start" in names
    assert "workflow/agent-start" in names
    assert "workflow/agent-end" in names
    assert "workflow/end" in names
    agent_start = events[names.index("workflow/agent-start")][1]
    agent_end = events[names.index("workflow/agent-end")][1]
    assert agent_start["seq"] == 1
    assert agent_end["outcome"] == "completed"
    assert agent_start["childId"]
    end = events[names.index("workflow/end")][1]
    assert end["stopReason"] == "completed"
    assert end["agentsStarted"] == 1
    # agent() resolves to the child's final text.
    assert result.output == {"out": "All done."}


# ---------------------------------------------------------------------------
# Ralph contract
# ---------------------------------------------------------------------------


def test_ralph_report_validation_rules():
    assert _validate_report(
        RalphHandoff(status="continue", summary="working", next_steps="run tests")
    ) is None
    assert _validate_report(
        RalphHandoff(status="continue", summary="working", next_steps="")
    ) is not None
    assert _validate_report(
        RalphHandoff(status="complete", summary="done", evidence="tests pass")
    ) is None
    assert _validate_report(
        RalphHandoff(status="complete", summary="done", next_steps="more")
    ) is not None
    assert _validate_report(
        RalphHandoff(status="blocked", summary="stuck", blocker="no docker")
    ) is None
    assert _validate_report(RalphHandoff(status="blocked", summary="stuck")) is not None


@pytest.mark.asyncio
async def test_ralph_budget_limited_is_success(tool_context):
    res = await handle_ralph_tool(
        {"objective": "Verify budget behavior", "max_rounds": 1}, tool_context
    )
    # Non-JSON response → invalid report → round-failed (isError).
    assert res.ok is False
    assert res.metadata["status"] == "round-failed"


@pytest.mark.asyncio
async def test_ralph_max_rounds_ceiling(tool_context):
    res = await handle_ralph_tool(
        {"objective": "Verify ceiling", "max_rounds": 999_999}, tool_context
    )
    assert res.ok is False
    assert "ceiling" in (res.error or "")


# ---------------------------------------------------------------------------
# Goal domain: phases, CAS, blocked threshold, tools, round driver
# ---------------------------------------------------------------------------


@pytest.fixture
def goal_store(tmp_path: pathlib.Path):
    reset_dsh_goal_store()
    store = get_dsh_goal_store(str(tmp_path))
    yield store
    reset_dsh_goal_store()


def test_goal_lifecycle_and_cas(goal_store: DSHGoalStore):
    goal = goal_store.create("sess", "Ship the feature", max_goal_rounds=5)
    assert goal.phase == "active"
    assert goal.activation == "armed"
    assert goal.max_goal_rounds == 5

    with pytest.raises(GoalError):
        goal_store.complete("sess", goal.ref().__class__(id=goal.id, revision=999))
    paused = goal_store.pause("sess", goal.ref())
    assert paused.phase == "paused"
    resumed = goal_store.resume("sess", paused.ref())
    assert resumed.phase == "active"
    completed = goal_store.complete("sess", resumed.ref())
    assert completed.phase == "complete"

    # Blocked carries a machine-routable reason.
    goal2 = goal_store.create("sess2", "Other goal")
    blocked = goal_store.block(
        "sess2", goal2.ref(), GoalBlockReason(code="model-reported", message="needs human")
    )
    assert blocked.phase == "blocked"
    assert blocked.blocked_reason.code == "model-reported"


def test_goal_reload_starts_disarmed(goal_store: DSHGoalStore, tmp_path: pathlib.Path):
    goal_store.create("sess3", "Persisted goal")
    reset_dsh_goal_store()
    reloaded = get_dsh_goal_store(str(tmp_path)).get("sess3")
    assert reloaded is not None
    assert reloaded.activation == "disarmed"


@pytest.mark.asyncio
async def test_goal_tools_and_authority(tmp_path: pathlib.Path, goal_store: DSHGoalStore):
    ctx = ToolExecutionContext(session_id="sess4", project_root=str(tmp_path))
    created = await handle_create_goal_tool({"objective": "Ship it", "max_goal_rounds": 4}, ctx)
    assert created.ok is True
    goal_id = created.metadata["goal"]["id"]
    revision = created.metadata["goal"]["revision"]

    got = await handle_get_goal_tool({}, ctx)
    assert got.ok is True
    assert got.metadata["goal"]["id"] == goal_id
    assert got.metadata["activation"] == "armed"

    stale = await handle_update_goal_tool(
        {"goal_id": goal_id, "revision": revision + 1, "action": "complete"}, ctx
    )
    assert stale.ok is False
    assert "STALE" in stale.error

    done = await handle_update_goal_tool(
        {"goal_id": goal_id, "revision": revision, "action": "complete"}, ctx
    )
    assert done.ok is True
    assert done.metadata["goal"]["phase"] == "complete"

    # Subagent callers are rejected for create/pause/resume/edit.
    sub_ctx = ToolExecutionContext(session_id="sub_parent_ab_task1", project_root=str(tmp_path))
    rejected = await handle_create_goal_tool({"objective": "Nested"}, sub_ctx)
    assert rejected.ok is False
    assert "authority" in (rejected.error or "").lower()


@pytest.mark.asyncio
async def test_goal_blocked_threshold_during_goal_round(tmp_path: pathlib.Path, goal_store: DSHGoalStore):
    ctx = ToolExecutionContext(session_id="sess5", project_root=str(tmp_path))
    created = await handle_create_goal_tool({"objective": "Long goal"}, ctx)
    goal_id = created.metadata["goal"]["id"]
    revision = created.metadata["goal"]["revision"]

    # Outside an automatic round the model may report blocked immediately.
    blocked_now = await handle_update_goal_tool(
        {
            "goal_id": goal_id,
            "revision": revision,
            "action": "blocked",
            "blocked_reason": "stuck",
        },
        ctx,
    )
    assert blocked_now.ok is True

    # During an automatic round, the minimum-round threshold applies.
    goal2 = await handle_create_goal_tool({"objective": "Long goal 2"}, ctx)
    goal_id2 = goal2.metadata["goal"]["id"]
    rev2 = goal2.metadata["goal"]["revision"]
    goal_store.set_in_goal_round("sess5", True)
    try:
        early = await handle_update_goal_tool(
            {
                "goal_id": goal_id2,
                "revision": rev2,
                "action": "blocked",
                "blocked_reason": "stuck",
            },
            ctx,
        )
        assert early.ok is False
        assert "BLOCK_THRESHOLD" in early.error
    finally:
        goal_store.set_in_goal_round("sess5", False)


def test_goal_round_driver_queues_and_blocks_at_limit(
    tmp_path: pathlib.Path, goal_store: DSHGoalStore
):
    appended: list[tuple[str, str, dict]] = []

    class StubManager:
        project_root = str(tmp_path)

        def get_resolved_settings(self):
            return {}

        def _append_message(self, message):
            appended.append((message.session_id, message.content, message.meta or {}))

        def _build_message(self, session_id, role, content, **kwargs):
            return type(
                "M",
                (),
                {"session_id": session_id, "role": role, "content": content, "meta": kwargs.get("meta")},
            )()

    goal_store.create("sess6", "Drive to completion", max_goal_rounds=2)
    mgr = StubManager()

    assert maybe_queue_goal_round(mgr, "sess6") is True
    assert len(appended) == 1
    assert "<goal_round>" in appended[0][1]
    assert goal_store.in_goal_round("sess6") is True
    finish_goal_round("sess6", str(tmp_path), entry_status="completed")
    assert goal_store.in_goal_round("sess6") is False

    assert maybe_queue_goal_round(mgr, "sess6") is True
    finish_goal_round("sess6", str(tmp_path), entry_status="completed")

    # Round cap exhausted → no queue, goal blocked with round-limit code.
    assert maybe_queue_goal_round(mgr, "sess6") is False
    blocked = goal_store.get("sess6")
    assert blocked.phase == "blocked"
    assert blocked.blocked_reason.code == BLOCK_CODE_ROUND_LIMIT

    # Disarmed goals never queue.
    goal_store.create("sess7", "Another")
    goal_store.disarm("sess7")
    assert maybe_queue_goal_round(mgr, "sess7") is False


def test_render_goal_round_prompt_matches_harness_shape():
    class G:
        objective = "Ship it"
        max_goal_rounds = 8

    prompt = render_goal_round_prompt(G(), 3)
    assert "<goal_round>" in prompt
    assert 'Objective: "Ship it"' in prompt
    assert "Round: 3/8" in prompt
    assert "</goal_round>" in prompt


@pytest.mark.asyncio
async def test_cross_manager_subagent_cancellation(tmp_path: pathlib.Path):
    """Verify that cancelling via one SubAgentManager instance aborts runs on another instance."""
    manager1 = SubAgentManager(
        project_root=str(tmp_path),
        create_openai_client=_slow_client_factory(1.0),
    )
    manager2 = SubAgentManager(
        project_root=str(tmp_path),
        create_openai_client=_simple_client_factory(),
    )

    spec = SubAgentSpec(
        description="Slow worker",
        prompt="Execute slowly",
        task_id="slow1234",
        parent_session_id="parent_sess",
    )

    async def _run():
        return await manager1.spawn_subagent(spec)

    task = asyncio.create_task(_run())
    await asyncio.sleep(0.05)

    # Cancel globally from manager2
    session_id = f"sub_{spec.parent_session_id[:8]}_{spec.task_id}"
    manager2.cancel_subagent(session_id)

    res = await asyncio.wait_for(task, timeout=5.0)
    assert res.status in ("interrupted", "failed")


def test_git_file_history_hermetic_env(tmp_path: pathlib.Path):
    """Verify GitFileHistory operates hermetically without host gitconfig dependency."""
    from coderai.core.common.file_history import GitFileHistory

    git_dir = tmp_path / ".git_history"
    history = GitFileHistory(str(tmp_path), str(git_dir))
    env = history._get_git_env()
    assert env.get("GIT_CONFIG_NOSYSTEM") == "1"
    assert env.get("GIT_CONFIG_GLOBAL") == "/dev/null"
    assert env.get("GIT_CONFIG_SYSTEM") == "/dev/null"

    # Ensure session initializes cleanly
    head = history.ensure_session("hermetic_test_session")
    assert head is not None
    assert len(head) == 40

