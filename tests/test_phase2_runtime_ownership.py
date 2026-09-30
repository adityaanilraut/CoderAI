"""Project-local schedules and manager-owned shutdown through real runtime objects."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from coderai.background.store import JobStore
from coderai.cli.session_factory import close_session_manager
from coderai.schedule import ScheduleManager, get_schedule_manager
from coderai.soul.session.manager import SessionManager
from coderai.subagents.core import AgentHandle, AgentRegistry
from coderai.tools.legacy.schedule import (
    handle_schedule_create_tool,
    handle_schedule_delete_tool,
    handle_schedule_list_tool,
)
from coderai.tools.legacy.types import ToolExecutionContext


def _manager(root):
    root.mkdir(parents=True, exist_ok=True)
    return SessionManager(
        project_root=str(root), create_openai_client=lambda: {}, get_resolved_settings=lambda: {}
    )


def _context(manager, sid):
    return ToolExecutionContext(
        session_id=sid, project_root=manager.project_root, session_manager=manager
    )


def test_managers_and_tool_routes_keep_project_persistence_separate(tmp_path, isolated_home):
    a = _manager(tmp_path / "a")
    a_path = a.schedule_manager.storage_path
    a_first = handle_schedule_create_tool(
        {"prompt": "A first", "after_seconds": 30}, _context(a, "A")
    )
    b = _manager(tmp_path / "b")
    try:
        assert a.schedule_manager is not b.schedule_manager
        assert a.schedule_manager.storage_path == a_path
        b_first = handle_schedule_create_tool(
            {"prompt": "B first", "after_seconds": 30}, _context(b, "B")
        )
        a_second = handle_schedule_create_tool(
            {"prompt": "A second", "after_seconds": 30}, _context(a, "A")
        )
        assert a_first.ok and b_first.ok and a_second.ok
        assert a_first.metadata["id"] == b_first.metadata["id"] == "sched_1"
        assert a_second.metadata["id"] == "sched_2"
        assert [
            r["prompt"]
            for r in handle_schedule_list_tool({}, _context(a, "A")).metadata["schedules"]
        ] == ["A first", "A second"]
        assert [
            r["prompt"]
            for r in handle_schedule_list_tool({}, _context(b, "B")).metadata["schedules"]
        ] == ["B first"]
        assert [r["prompt"] for r in json.loads(Path(a_path).read_text())["schedules"]] == [
            "A first",
            "A second",
        ]
        assert [
            r.prompt for r in ScheduleManager(b.schedule_manager.storage_path).list_schedules()
        ] == ["B first"]
        assert handle_schedule_delete_tool({"schedule_id": "sched_1"}, _context(a, "A")).ok
        assert [r.prompt for r in b.schedule_manager.list_schedules()] == ["B first"]
    finally:
        a.dispose()
        b.dispose()


def test_canonical_schedule_cache_shares_one_project_without_retargeting(tmp_path):
    path = tmp_path / "project" / "schedule.json"
    a = get_schedule_manager(str(path))
    assert get_schedule_manager(str(path.parent / ".." / "project" / "schedule.json")) is a
    default = get_schedule_manager()
    other = get_schedule_manager(str(tmp_path / "other.json"))
    assert other is not a and default is not a
    assert a.storage_path == str(path.resolve())
    with ThreadPoolExecutor(max_workers=8) as executor:
        copies = list(executor.map(lambda _: get_schedule_manager(str(path)), range(32)))
    assert all(copy is a for copy in copies)


def test_schedule_tools_without_manager_use_their_context_project(tmp_path, isolated_home):
    contexts = [ToolExecutionContext("s", str(tmp_path / p)) for p in ("a", "b")]
    for n, context in enumerate(contexts):
        assert handle_schedule_create_tool({"prompt": str(n), "after_seconds": 30}, context).ok
    for n, context in enumerate(contexts):
        records = handle_schedule_list_tool({}, context).metadata["schedules"]
        assert [r["prompt"] for r in records] == [str(n)]


def test_schedule_delete_cannot_cancel_another_sessions_reminder(tmp_path, isolated_home):
    manager = _manager(tmp_path / "project")
    try:
        record = handle_schedule_create_tool(
            {"prompt": "private", "after_seconds": 30}, _context(manager, "A")
        )
        assert not handle_schedule_delete_tool(
            {"schedule_id": record.metadata["id"]}, _context(manager, "B")
        ).ok
        assert manager.schedule_manager.list_schedules("A")
    finally:
        manager.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("async_close", [False, True])
async def test_closing_manager_cancels_only_owned_jobs_and_descendant_agents(
    tmp_path, isolated_home, monkeypatch, async_close
):
    store = JobStore()
    registry = AgentRegistry()
    monkeypatch.setattr("coderai.background.manager._STORE", store)
    monkeypatch.setattr("coderai.subagents.core._registry", registry)
    a, b = _manager(tmp_path / "a"), _manager(tmp_path / "b")
    a._active_session_id, b._active_session_id = "A", "B"
    a.session_controllers["A"] = asyncio.Event()
    b.session_controllers["B"] = asyncio.Event()
    entered = [asyncio.Event() for _ in range(3)]
    release = asyncio.Event()

    async def worker(index):
        entered[index].set()
        await release.wait()

    handles = [
        AgentHandle(
            id="child-A",
            parent_session_id="A",
            description="A",
            mode="continuable",
            run_session_id="A-child",
        ),
        AgentHandle(
            id="grandchild-A",
            parent_session_id="A-child",
            description="A child",
            mode="continuable",
            run_session_id="A-grandchild",
        ),
        AgentHandle(
            id="child-B",
            parent_session_id="B",
            description="B",
            mode="continuable",
            run_session_id="B-child",
        ),
    ]
    tasks = []
    cancelled_jobs = []
    for i, handle in enumerate(handles):
        handle.task = asyncio.create_task(worker(i))
        tasks.append(handle.task)
        registry.register(handle)
    for sid in ("A", "A-child", "A-grandchild", "B"):
        store.start(job_id=f"job-{sid}", session_id=sid, kind="test", label=sid)
        store.register_cancel_callback(f"job-{sid}", lambda s=sid: cancelled_jobs.append(s))
    try:
        await asyncio.gather(*(e.wait() for e in entered))
        if async_close:
            await close_session_manager(a)
        else:
            a.dispose()
            await asyncio.wait(tasks[:2], timeout=1)
        assert set(cancelled_jobs) == {"A", "A-child", "A-grandchild"}
        assert all(task.cancelled() for task in tasks[:2])
        assert not tasks[2].done() and not handles[2].killed
        assert store.get("job-B").status == "running"
        assert not b.session_controllers["B"].is_set()
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        a.dispose()
        b.dispose()


def test_closing_manager_releases_idle_teammate_contexts_and_own_spills(
    tmp_path, isolated_home, monkeypatch
):
    from coderai.teams.manager import TeamManager
    from coderai.spill import session_dir

    teams = TeamManager()
    monkeypatch.setattr("coderai.teams.manager._global_team_manager", teams)
    spill_root = tmp_path / "spills"
    spill_root.mkdir()
    monkeypatch.setattr("coderai.spill._default_root", spill_root)
    managers = [_manager(tmp_path / label) for label in ("a", "b")]
    teammates = []
    for manager, sid in zip(managers, ("A", "B"), strict=True):
        manager._active_session_id = sid
        teammates.append(
            teams.spawn_teammate(
                name=sid, role="default", auto_start=False, execution_context=_context(manager, sid)
            )
        )
        session_dir(spill_root, sid).mkdir(parents=True)
    try:
        managers[0].dispose()
        assert teammates[0].teammate_id not in teams._execution_contexts
        assert teams.channel.get_mailbox(teammates[0].teammate_id) is None
        assert teammates[1].teammate_id in teams._execution_contexts
        assert teams.channel.get_mailbox(teammates[1].teammate_id) is not None
        assert not session_dir(spill_root, "A").exists()
        assert session_dir(spill_root, "B").exists()
    finally:
        for manager in managers:
            manager.dispose()


@pytest.mark.asyncio
async def test_print_stream_json_drains_only_the_created_session(tmp_path, isolated_home, capsys):
    from coderai.ui.shell.app import _run_once
    from coderai.wire.emitter import get_emitter

    get_emitter().text("UNRELATED LEGACY OUTPUT")
    manager = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": object(), "model": "gpt-4o"},
        get_resolved_settings=lambda: {"model": "gpt-4o"},
    )
    manager._create_completion_with_retry = AsyncMock(
        return_value={"choices": [{"message": {"content": "PRIVATE PRINT ANSWER"}}]}
    )
    try:
        assert await _run_once(manager, "answer", yes=True, output_format="stream-json") == 0
        output = capsys.readouterr().out
        assert "PRIVATE PRINT ANSWER" in output, (
            manager._get_entry(manager._active_session_id),
            manager._create_completion_with_retry.await_count,
        )
        assert "UNRELATED LEGACY OUTPUT" not in output
        assert all(isinstance(json.loads(line), dict) for line in output.splitlines())
    finally:
        manager.dispose()


def test_reading_another_session_does_not_claim_its_resources(tmp_path, isolated_home, monkeypatch):
    store = JobStore()
    monkeypatch.setattr("coderai.background.manager._STORE", store)
    manager = _manager(tmp_path / "project")
    manager._session_states["foreign"] = object()
    manager._seq_counters["foreign"] = 7
    store.start(job_id="foreign-job", session_id="foreign", kind="test", label="other manager")
    manager.dispose()
    assert store.get("foreign-job").status == "running"


def test_dispose_kills_tracked_processes_only_in_owned_sessions(
    tmp_path, isolated_home, monkeypatch
):
    killed = []
    monkeypatch.setattr("coderai.utils.subprocess_env.kill_process_tree", killed.append)
    managers = [_manager(tmp_path / label) for label in ("a", "b")]
    for manager, sid, pid in zip(managers, ("A", "B"), (123456, 234567), strict=True):
        manager._record_index_entry(manager._build_index_entry(sid, "process", status="running"))
        manager._active_session_id = sid
        manager._track_process_start(sid, pid, "a harmless fake process")
    try:
        managers[0].dispose()
        assert killed == [123456]
        assert managers[0]._get_entry("A")["processes"] == {}
        assert "234567" in managers[1]._get_entry("B")["processes"]
    finally:
        for manager in managers:
            manager.dispose()


@pytest.mark.asyncio
async def test_async_close_cannot_recreate_emitter_from_late_committed_message(
    tmp_path, isolated_home
):
    manager = _manager(tmp_path / "project")
    emitter = manager.get_event_emitter("owned")
    side = emitter.ui_side(merge=False)
    try:
        await close_session_manager(manager)
        manager.on_assistant_message(
            manager._build_assistant("owned", "late private output", None), True
        )
        assert not manager._event_emitters
        assert side._queue.closed and emitter.buffered() == []
        with pytest.raises(RuntimeError, match="closed"):
            manager.get_event_emitter("owned")
    finally:
        manager.dispose()


@pytest.mark.asyncio
async def test_delete_active_session_cancels_work_and_releases_only_its_stream(
    tmp_path, isolated_home, monkeypatch
):
    manager = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": object(), "model": "gpt-4o"},
        get_resolved_settings=lambda: {"model": "gpt-4o"},
    )
    entered = asyncio.Event()
    killed = []
    monkeypatch.setattr("coderai.utils.subprocess_env.kill_process_tree", killed.append)

    async def provider(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    manager._create_completion_with_retry = AsyncMock(side_effect=provider)
    turn = asyncio.create_task(manager.create_session("wait", skills=[]))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        sid = manager._active_session_id
        emitter = manager.get_event_emitter(sid)
        side = emitter.ui_side(merge=False)
        other = manager.get_event_emitter("sibling")
        other_side = other.ui_side(merge=False)
        manager._track_process_start(sid, 123456, "harmless fake process")
        assert manager.delete_session(sid)
        await asyncio.gather(turn, return_exceptions=True)
        assert turn.cancelled() and killed == [123456]
        assert side._queue.closed and not other_side._queue.closed
        assert sid not in manager._event_emitters and emitter.buffered() == []
        manager.on_assistant_message(
            manager._build_assistant(sid, "late deleted content", None), True
        )
        manager._append_message(manager._build_message(sid, "assistant", "late persisted content"))
        assert sid not in manager._event_emitters
        assert not manager.session_store.messages_path(sid).exists()
    finally:
        if not turn.done():
            turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
        manager.dispose()
