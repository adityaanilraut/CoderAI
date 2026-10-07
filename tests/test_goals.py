"""Goal contract, persistence, bounded execution, and session lifecycle regressions."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from coderai.events import GOAL_STATE
from coderai.goals.core import GoalStore, GoalStorageError, get_goal_store, handle_goal_tool
from coderai.soul.session.manager import SessionManager
from coderai.tools.legacy.executor import ToolExecutor
from coderai.tools.legacy.types import ToolExecutionContext, ToolExecutionHooks
from coderai.ui.shell.dispatch import ShellContext, SlashAction, cmd_goal


@pytest.fixture
def manager(tmp_path, isolated_home, monkeypatch):
    monkeypatch.setenv("CODERAI_SHARE_DIR", str(tmp_path / "share"))
    manager = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": object(), "model": "gpt-6-sol"},
        get_resolved_settings=lambda: {"model": "gpt-6-sol", "maxStepsPerTurn": 10},
    )
    manager._record_index_entry(
        manager._build_index_entry("s", "test", status="completed", plan_mode=False)
    )
    manager.file_history.ensure_session("s")
    manager._append_message(
        manager._build_message("s", "user", "initial request", meta={"goalSnapshot": []})
    )
    yield manager
    manager.close_event_streams()


def tool_call(**args):
    return {
        "id": "g",
        "type": "function",
        "function": {"name": "goal", "arguments": json.dumps(args)},
    }


def response(content="", calls=None):
    return {
        "choices": [
            {"message": {"role": "assistant", "content": content, "tool_calls": calls or []}}
        ]
    }


def context(tmp_path):
    return ToolExecutionContext(session_id="s", project_root=str(tmp_path))


@pytest.mark.asyncio
async def test_registered_tool_creates_and_manages_real_goals(tmp_path):
    executor = ToolExecutor(str(tmp_path))
    result = await executor.execute_tool_call(
        "s",
        tool_call(
            action="create",
            objective="Repair login",
            description="Verify login",
            milestones=["reproduce", "fix", "test"],
            max_rounds=3,
        ),
    )
    assert result.ok, result.error
    goal_id = result.metadata["id"]
    for args in (
        {"action": "update", "completed_milestones": 1, "notes": "reproduction evidence"},
        {"action": "pause"},
        {"action": "start"},
        {"action": "complete"},
    ):
        result = await executor.execute_tool_call("s", tool_call(goal_id=goal_id, **args))
        assert result.ok, result.error
    goal = get_goal_store(str(tmp_path)).get("s", goal_id)
    assert goal.status == "completed" and goal.description == "Verify login"
    assert goal.milestones == ("reproduce", "fix", "test") and goal.completed_milestones == 1
    assert goal.notes == "reproduction evidence"
    status = await executor.execute_tool_call("s", tool_call(action="status"))
    assert status.ok and "[x] reproduce" in status.output


@pytest.mark.asyncio
async def test_status_allowed_but_goal_mutations_denied_in_plan_mode(tmp_path):
    executor = ToolExecutor(str(tmp_path))
    hooks = ToolExecutionHooks(plan_mode=True)
    status = await executor.execute_tool_call("s", tool_call(action="status"), hooks)
    create = await executor.execute_tool_call(
        "s", tool_call(action="create", objective="work"), hooks
    )
    assert status.ok and not create.ok and "plan mode" in create.error
    assert get_goal_store(str(tmp_path)).list("s") == []


@pytest.mark.parametrize(
    "args",
    [
        {"action": "resume"},
        {"action": "create", "objective": " "},
        {"action": "create", "objective": "work", "max_rounds": "bad"},
        {"action": "create", "objective": "work", "max_rounds": 0},
        {"action": "create", "objective": "work", "max_rounds": True},
        {"action": "create", "objective": "work", "milestones": [""]},
        {"action": "update", "status": "in_progress"},
        {"action": "update", "objective": ""},
        {"action": "update", "completed_milestones": 4},
        {"action": "update"},
    ],
)
def test_handler_rejects_invalid_inputs_without_mutation(tmp_path, args):
    store = get_goal_store(str(tmp_path))
    store.create("s", "original", milestones=["one"])
    before = store.snapshot("s")
    result = handle_goal_tool(args, context(tmp_path))
    assert not result.ok and result.error
    assert store.snapshot("s") == before


def test_environment_budget_applies_to_store_and_handler(tmp_path, monkeypatch):
    monkeypatch.setenv("CODERAI_GOAL_MAX_ROUNDS", "3")
    store = get_goal_store(str(tmp_path))
    assert store.create("s", "first").max_rounds == 3
    result = handle_goal_tool({"action": "create", "objective": "second"}, context(tmp_path))
    assert result.ok and result.metadata["max_rounds"] == 3


def test_normal_reload_and_legacy_round_status_migration(tmp_path):
    store = GoalStore(tmp_path / "goals")
    goal = store.create("s", "original")
    data = goal.to_dict()
    data.pop("version")
    data.update(round=3, status="done")
    store._path("s").write_text(json.dumps([data]))
    migrated = GoalStore(store.root).get("s", goal.id)
    assert migrated.round == 2 and migrated.status == "completed"
    store.update("s", goal.id, notes="migrated")
    assert json.loads(store._path("s").read_text())[0]["version"] == 2


def test_missing_target_does_not_pause_active_goal(tmp_path):
    store = GoalStore(tmp_path / "goals")
    goal = store.create("s", "work")
    assert store.update("s", "missing", status="running") is None
    assert store.get_active_goal("s") == goal


def test_failed_write_does_not_change_observed_state(tmp_path):
    store = GoalStore(tmp_path / "goals")
    goal = store.create("s", "work")
    with patch("coderai.goals.core.atomic_json_write", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            store.update("s", goal.id, status="completed")
        with pytest.raises(OSError):
            store.create("s", "second")
        with pytest.raises(OSError):
            store.record_attempt("s", goal.id)
    assert store.list("s") == [goal]


def test_returns_immutable_detached_snapshots(tmp_path):
    store = GoalStore(tmp_path / "goals")
    goal = store.create("s", "first", milestones=["one"])
    with pytest.raises(FrozenInstanceError):
        goal.status = "completed"
    goals = store.list("s")
    goals.clear()
    snapshot = store.snapshot("s")
    snapshot[0]["milestones"].append("extra")
    assert store.get("s", goal.id).milestones == ("one",)
    assert store.get_active_goal("s") == goal


def test_two_store_instances_reload_without_losing_new_goals(tmp_path):
    first = GoalStore(tmp_path / "goals")
    goal = first.create("s", "one")
    second = GoalStore(first.root)
    assert second.list("s") == [goal]
    new = first.create("s", "two")
    second.update("s", goal.id, notes="second writer")
    assert {g.id for g in first.list("s")} == {goal.id, new.id}
    with pytest.raises(ValueError, match="revision changed"):
        first.update("s", goal.id, expected_revision=goal.revision, notes="stale")


def test_concurrent_process_writes_preserve_all_goals(tmp_path):
    script = "from coderai.goals.core import GoalStore; import sys; s=GoalStore(sys.argv[1]); [s.create('s', sys.argv[2]+str(i), status='pending') for i in range(10)]"
    root = tmp_path / "goals"
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(root), name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for name in ("one", "two")
    ]
    for process in processes:
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, (stdout, stderr)
    assert len(GoalStore(root).list("s")) == 20


def test_invalid_record_is_reported_valid_records_visible_and_writes_blocked(tmp_path, caplog):
    store = GoalStore(tmp_path / "goals")
    goal = store.create("s", "valid")
    raw = json.dumps([goal.to_dict(), {**goal.to_dict(), "id": "bad", "revision": "bad"}])
    store._path("s").write_text(raw)
    assert store.list("s") == [goal]
    assert "Original file preserved" in caplog.text
    with pytest.raises(GoalStorageError):
        store.create("s", "replacement")
    assert store._path("s").read_text() == raw
    store.delete_session("s")
    assert not store._path("s").exists()


@pytest.mark.parametrize("raw", ["not JSON", "{}", "null"])
def test_invalid_file_is_never_replaced(tmp_path, raw):
    store = GoalStore(tmp_path / "goals")
    store.root.mkdir()
    store._path("s").write_text(raw)
    with pytest.raises(GoalStorageError):
        store.create("s", "replacement")
    assert store._path("s").read_text() == raw


@pytest.mark.asyncio
async def test_slash_add_creates_real_session_and_pending_goal(manager):
    ctx = ShellContext(mgr=manager)
    assert await cmd_goal(ctx, "add Repair login") == SlashAction.HANDLED
    assert ctx.session_id and ctx.session_id != "default"
    goal = get_goal_store(manager.project_root).list(ctx.session_id)[0]
    assert goal.status == "pending" and goal.objective == "Repair login"
    assert manager.get_session(ctx.session_id)
    assert any(
        row.get("type") == GOAL_STATE for row in manager.session_store.read_rows(ctx.session_id)
    )


@pytest.mark.asyncio
async def test_start_runs_until_budget_and_reports_exhaustion(manager):
    store = get_goal_store(manager.project_root)
    goal = store.create("s", "work", max_rounds=2, status="pending")
    calls = []

    async def activate(sid):
        calls.append(sid)
        manager.goal_runner.on_turn_end(sid, "natural")

    manager._activate = activate
    await cmd_goal(ShellContext(mgr=manager, session_id="s"), "start " + goal.id)
    latest = store.get("s", goal.id)
    assert latest.round == 2 and latest.status == "failed" and calls == ["s", "s"]
    assert "FAILED" in manager.session_store.read_rows("s")[-1]["data"]["content"]


@pytest.mark.parametrize(
    "reason", ["error", "interrupted", "cancelled", "refusal", "hook", "tool_call_repeat"]
)
@pytest.mark.asyncio
async def test_bad_outcomes_pause_without_debit_or_retry(manager, reason):
    store = get_goal_store(manager.project_root)
    goal = store.create("s", "work", max_rounds=2)

    async def activate(sid):
        manager.goal_runner.on_turn_end(sid, reason)

    manager._activate = AsyncMock(side_effect=activate)
    await manager.goal_runner.start("s", goal.id)
    latest = store.get("s", goal.id)
    assert latest.status == "paused" and latest.round == 0
    assert manager._activate.await_count == 1


@pytest.mark.asyncio
async def test_cancellation_propagates_pauses_goal_and_releases_execution_lock(manager):
    store = get_goal_store(manager.project_root)
    goal = store.create("s", "work")
    started = asyncio.Event()

    async def activate(sid):
        started.set()
        await asyncio.Event().wait()

    manager._activate = activate
    task = asyncio.create_task(manager.goal_runner.start("s", goal.id))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.get("s", goal.id).status == "paused"
    assert store.get("s", goal.id).round == 0
    with store.execution_lock("s"):
        pass


@pytest.mark.asyncio
async def test_wait_suspends_current_round_then_resumes_it(manager):
    store = get_goal_store(manager.project_root)
    goal = store.create("s", "work", max_rounds=2)

    async def activate(sid):
        manager.goal_runner.on_turn_end(sid, "permission")

    manager._activate = AsyncMock(side_effect=activate)
    await manager.goal_runner.start("s", goal.id)
    assert store.get("s", goal.id).round == 0 and store.get("s", goal.id).status == "running"
    manager.goal_runner.on_turn_end("s", "natural")
    manager._activate = AsyncMock(
        side_effect=lambda sid: manager.goal_runner.on_turn_end(sid, "natural")
    )
    await manager.goal_runner.drain("s")
    assert store.get("s", goal.id).round == 2 and store.get("s", goal.id).status == "failed"
    assert manager._activate.await_count == 1


@pytest.mark.asyncio
async def test_unrelated_turns_never_consume_goal_rounds(manager):
    store = get_goal_store(manager.project_root)
    goal = store.create("s", "work")
    for reason in ("natural", "error", "interrupted"):
        manager.goal_runner.on_turn_end("s", reason)
        await manager.goal_runner.drain("s")
    assert store.get("s", goal.id) == goal


@pytest.mark.asyncio
async def test_goal_completion_stops_driver_preserves_notes(manager):
    store = get_goal_store(manager.project_root)
    goal = store.create("s", "work", notes="evidence")

    async def activate(sid):
        result = handle_goal_tool(
            {"action": "complete"},
            ToolExecutionContext(
                session_id=sid, project_root=manager.project_root, session_manager=manager
            ),
        )
        assert result.ok
        manager.goal_runner.on_turn_end(sid, "natural")

    manager._activate = AsyncMock(side_effect=activate)
    await manager.goal_runner.start("s", goal.id)
    latest = store.get("s", goal.id)
    assert latest.status == "completed" and latest.round == 1 and latest.notes == "evidence"
    assert manager._activate.await_count == 1


@pytest.mark.asyncio
async def test_goal_injection_refreshes_after_revision_and_compaction(manager):
    from coderai.soul.dynamic_injections.goals import GoalInjectionProvider

    store = get_goal_store(manager.project_root)
    provider = GoalInjectionProvider()
    soul = SimpleNamespace(manager=manager, session_id="s", is_subagent=False)
    assert await provider.get_injections([], soul) == []
    goal = store.create("s", "Visible objective", milestones=["test"])
    assert "Visible objective" in (await provider.get_injections([], soul))[0].content
    assert await provider.get_injections([], soul) == []
    store.update("s", goal.id, status="paused")
    assert "paused" in (await provider.get_injections([], soul))[0].content
    await provider.on_context_compacted()
    assert await provider.get_injections([], soul)


def test_fork_copies_goals_paused_and_undo_restores_checkpoint(manager):
    store = get_goal_store(manager.project_root)
    original_message = manager.list_session_messages("s")[0]
    goal = store.create("s", "work", milestones=["test"])
    forked = manager.fork_session("s")
    assert store.get(forked, goal.id).status == "paused"
    assert store.get(forked, goal.id).milestones == ("test",)
    partial = manager.fork_session("s", original_message.id)
    assert store.list(partial) == []
    store.update("s", goal.id, status="completed")
    assert manager.undo("s", original_message.id, mode="restore_conversation_only")
    assert store.list("s") == []


def test_delete_removes_goal_data_even_when_corrupt(manager):
    store = get_goal_store(manager.project_root)
    store.create("s", "work")
    store._path("s").write_text("bad JSON")
    assert manager.delete_session("s")
    assert not store._path("s").exists() and store.list("s") == []


@pytest.mark.asyncio
async def test_real_agent_loop_tool_creation_continues_and_completes(manager):
    responses = [
        response(
            calls=[
                tool_call(action="create", objective="work", max_rounds=2, milestones=["verify"])
            ]
        ),
        response("First round complete; more verification needed"),
        response(calls=[tool_call(action="complete", notes="verified")]),
        response("All verified"),
    ]
    manager._create_completion_with_retry = AsyncMock(side_effect=responses)
    await manager.reply_session("s", "Create a goal and finish it")
    goal = get_goal_store(manager.project_root).list("s")[0]
    assert goal.status == "completed" and goal.round == 2 and goal.notes == "verified"
    assert manager._create_completion_with_retry.await_count == 4
    assert any(
        row.get("type") == GOAL_STATE and "COMPLETED" in row["data"]["content"]
        for row in manager.session_store.read_rows("s")
    )


@pytest.mark.asyncio
async def test_permission_notice_preserves_pending_tool_call(manager):
    store = get_goal_store(manager.project_root)
    goal = store.create("s", "work")
    pending = manager._build_assistant("s", "Need approval", [tool_call(action="status")])
    manager._append_message(pending)
    manager.goal_runner._finish_attempt("s", goal.id, "permission")
    trailing = manager.message_converter.get_trailing_pending_tool_call_message(
        manager.list_session_messages("s")
    )
    assert trailing["message"].id == pending.id and trailing["toolCalls"]


@pytest.mark.asyncio
async def test_wait_recovery_after_runtime_restart(manager):
    from coderai.soul.session.goals import GoalRunner

    store = get_goal_store(manager.project_root)
    goal = store.create("s", "work", max_rounds=1)
    manager.goal_runner._finish_attempt("s", goal.id, "permission")
    manager._update_entry("s", lambda e: {**e, "status": "ask_permission"})
    manager.goal_runner = GoalRunner(manager)
    manager.goal_runner.before_turn("s")
    manager.goal_runner.on_turn_end("s", "natural")
    await manager.goal_runner.drain("s")
    assert store.get("s", goal.id).round == 1 and store.get("s", goal.id).status == "failed"


def test_interrupting_open_wait_pauses_goal_without_charge(manager):
    store = get_goal_store(manager.project_root)
    goal = store.create("s", "work")
    manager.goal_runner._finish_attempt("s", goal.id, "waiting")
    manager.interrupt_session("s")
    assert store.get("s", goal.id).status == "paused"
    assert store.get("s", goal.id).round == 0


@pytest.mark.asyncio
async def test_denied_approval_disables_autonomous_resume(manager):
    store = get_goal_store(manager.project_root)
    goal = store.create("s", "work")
    manager.goal_runner._finish_attempt("s", goal.id, "permission")
    manager._update_entry("s", lambda e: {**e, "status": "ask_permission"})
    manager._activate = AsyncMock()
    await manager.reply_session("s", permission_replies=[{"toolCallId": "t", "permission": "deny"}])
    assert store.get("s", goal.id).status == "paused" and store.get("s", goal.id).round == 0
    manager.goal_runner.on_turn_end("s", "natural")
    await manager.goal_runner.drain("s")
    assert manager._activate.await_count == 1


@pytest.mark.asyncio
async def test_fork_at_goal_event_restores_that_snapshot(manager):
    store = get_goal_store(manager.project_root)
    await cmd_goal(ShellContext(mgr=manager, session_id="s"), "add work")
    goal = store.list("s")[0]
    state_event = manager.session_store.read_rows("s")[-1]
    store.update("s", goal.id, status="completed")
    forked = manager.fork_session("s", state_event["seq"])
    assert store.get(forked, goal.id).status == "pending"


@pytest.mark.asyncio
async def test_shell_owns_new_goal_session_and_start_task(manager):
    import io
    from unittest.mock import MagicMock
    from rich.console import Console
    from coderai.ui.shell.controller import ShellController
    from coderai.ui.shell.preferences import DisplayPreferences

    prompt = MagicMock(shell_mode=False)
    prompt.session.app.output.get_size.return_value = SimpleNamespace(rows=24, columns=80)
    with patch.object(DisplayPreferences, "load", return_value=DisplayPreferences()):
        controller = ShellController(manager, Console(file=io.StringIO()), prompt=prompt)
    try:
        await controller.handle("/goal add work")
        sid = controller.view.session_id
        assert sid and sid != "default" and manager.get_session(sid)
        goal = get_goal_store(manager.project_root).list(sid)[0]
        started = asyncio.Event()
        release = asyncio.Event()

        async def start_goal(session_id, goal_id):
            assert session_id == sid and goal_id == goal.id
            started.set()
            await release.wait()

        manager.goal_runner.start = start_goal
        await controller.handle("/goal start " + goal.id)
        await started.wait()
        assert controller.running
        await controller.handle("/goal list")
        release.set()
        await controller.active
        await controller.settle()
        assert not controller.running
    finally:
        if controller.event_task:
            controller.event_task.cancel()
            await asyncio.gather(controller.event_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_two_runners_cannot_execute_same_session(manager):
    from coderai.soul.session.goals import GoalRunner

    store = get_goal_store(manager.project_root)
    goal = store.create("s", "work")
    started = asyncio.Event()

    async def activate(sid):
        started.set()
        await asyncio.Event().wait()

    manager._activate = activate
    task = asyncio.create_task(manager.goal_runner.start("s", goal.id))
    await started.wait()
    try:
        with pytest.raises(OSError):
            await GoalRunner(manager).start("s", goal.id)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_late_worker_cannot_recreate_deleted_goal_data(tmp_path):
    store = GoalStore(tmp_path / "goals")
    goal = store.create("s", "work")
    store.delete_session("s")
    with pytest.raises(GoalStorageError, match="deleted"):
        GoalStore(store.root).create("s", "late worker")
    with pytest.raises(GoalStorageError, match="deleted"):
        store.update("s", goal.id, notes="late update")
    assert not store._path("s").exists()


def test_unknown_versions_and_invalid_current_status_are_preserved(tmp_path):
    store = GoalStore(tmp_path / "goals")
    goal = store.create("s", "work")
    for changes in ({"version": 3}, {"status": "in_progress"}):
        raw = json.dumps([{**goal.to_dict(), **changes}])
        store._path("s").write_text(raw)
        with pytest.raises(GoalStorageError):
            store.create("s", "new")
        assert store._path("s").read_text() == raw


def test_failed_conversation_undo_preserves_goal_state(manager):
    store = get_goal_store(manager.project_root)
    original = manager.list_session_messages("s")[0]
    goal = store.create("s", "work")
    before = store.snapshot("s")
    with patch.object(manager, "_save_messages", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            manager.undo("s", original.id, mode="restore_conversation_only")
    assert store.snapshot("s") == before and store.get("s", goal.id)


@pytest.mark.asyncio
async def test_stop_hook_blocks_goal_continuation(manager):
    manager._create_completion_with_retry = AsyncMock(
        side_effect=[
            response(calls=[tool_call(action="create", objective="work", max_rounds=2)]),
            response("First turn finished"),
        ]
    )
    stopped = SimpleNamespace(stop=True, decision="deny", additional_context=[])
    with patch("coderai.hooks.runner.run_stop_async", new=AsyncMock(return_value=stopped)):
        await manager.reply_session("s", "Create and execute a goal")
    goal = get_goal_store(manager.project_root).list("s")[0]
    assert goal.status == "paused" and goal.round == 0
    assert manager._create_completion_with_retry.await_count == 2


@pytest.mark.asyncio
async def test_plan_mode_prevents_goal_execution(manager):
    store = get_goal_store(manager.project_root)
    goal = store.create("s", "work")
    manager._update_entry("s", lambda e: {**e, "planMode": True})
    manager._activate = AsyncMock()
    await manager.goal_runner.start("s", goal.id)
    assert store.get("s", goal.id).status == "paused" and store.get("s", goal.id).round == 0
    manager._activate.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelling_older_goal_does_not_stop_current_run(manager):
    store = get_goal_store(manager.project_root)
    old = store.create("s", "old")
    new = store.create("s", "new", max_rounds=1)
    started, release = asyncio.Event(), asyncio.Event()

    async def activate(sid):
        started.set()
        await release.wait()
        manager.goal_runner.on_turn_end(sid, "natural")

    manager._activate = activate
    task = asyncio.create_task(manager.goal_runner.start("s", new.id))
    await started.wait()
    await cmd_goal(ShellContext(mgr=manager, session_id="s"), "cancel " + old.id)
    assert store.get("s", new.id).status == "running"
    assert manager._get_entry("s")["status"] == "completed"
    release.set()
    await task
    assert store.get("s", new.id).round == 1


@pytest.mark.asyncio
async def test_running_goal_cannot_replace_itself_to_reset_budget(manager):
    store = get_goal_store(manager.project_root)
    goal = store.create("s", "work", max_rounds=1)

    async def activate(sid):
        result = handle_goal_tool(
            {"action": "create", "objective": "reset budget"},
            ToolExecutionContext(
                session_id=sid, project_root=manager.project_root, session_manager=manager
            ),
        )
        assert not result.ok and "pending goal" in result.error
        manager.goal_runner.on_turn_end(sid, "natural")

    manager._activate = activate
    await manager.goal_runner.start("s", goal.id)
    assert len(store.list("s")) == 1 and store.get("s", goal.id).status == "failed"


@pytest.mark.asyncio
async def test_cancellation_not_masked_by_failed_pause_write(manager, caplog):
    store = get_goal_store(manager.project_root)
    goal = store.create("s", "work")
    started = asyncio.Event()

    async def activate(sid):
        started.set()
        await asyncio.Event().wait()

    manager._activate = activate
    task = asyncio.create_task(manager.goal_runner.start("s", goal.id))
    await started.wait()
    with patch("coderai.goals.core.atomic_json_write", side_effect=OSError("disk full")):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert "Could not persist goal pause" in caplog.text
    with store.execution_lock("s"):
        pass
    assert not manager.goal_runner.is_driving("s")


@pytest.mark.asyncio
async def test_slash_pause_cancels_a_blocked_provider_call(manager):
    store = get_goal_store(manager.project_root)
    goal = store.create("s", "work")
    started = asyncio.Event()

    async def completion(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    manager._create_completion_with_retry = completion
    task = asyncio.create_task(manager.goal_runner.start("s", goal.id))
    await asyncio.wait_for(started.wait(), 2)
    await cmd_goal(ShellContext(mgr=manager, session_id="s"), "pause " + goal.id)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert store.get("s", goal.id).status == "paused" and store.get("s", goal.id).round == 0
