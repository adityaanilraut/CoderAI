"""Milestone 3 tests for explicit run/session recovery isolation."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coderAI.core.execution_context import (
    PermissionPolicySnapshot,
    create_run_context,
    execution_context_scope,
    get_execution_context,
)
from coderAI.core.services import get_services, services_scope
from coderAI.core.tool_executor import ToolExecutor
from coderAI.tools.undo import FileBackupStore, get_backup_store


def _context(tmp_path, session_id: str, *, tools: frozenset[str] = frozenset()):
    store = FileBackupStore(
        session_id=session_id,
        backup_root=str(tmp_path / "backups"),
    )
    ctx = create_run_context(
        workspace_root=str(tmp_path),
        permission_policy=PermissionPolicySnapshot(allowed_tools=tools),
    )
    return replace(ctx, session_id=session_id, checkpoint_store=store)


def test_run_context_is_immutable_and_workspace_identity_is_stable(tmp_path) -> None:
    first = create_run_context(workspace_root=str(tmp_path))
    second = create_run_context(workspace_root=str(tmp_path / "."))

    assert first.run_id != second.run_id
    assert first.workspace_id == second.workspace_id
    with pytest.raises(FrozenInstanceError):
        first.session_id = "other"  # type: ignore[misc]


def test_nested_scope_preserves_session_store_and_restores_parent(tmp_path) -> None:
    parent = _context(tmp_path, "session_parent")

    with execution_context_scope(run_context=parent):
        with execution_context_scope("child-tool", isolation_domain="browser"):
            active = get_execution_context()
            assert active.agent_id == "child-tool"
            assert active.session_id == "session_parent"
            assert active.checkpoint_store is parent.checkpoint_store
        assert get_execution_context() == parent


def test_agent_tool_scope_preserves_pinned_delegation_domain(tmp_path) -> None:
    child = replace(_context(tmp_path, "session_child"), isolation_domain="read_only")

    with execution_context_scope(run_context=child):
        with execution_context_scope("child-agent"):
            assert get_execution_context().isolation_domain == "read_only"


@pytest.mark.asyncio
async def test_concurrent_agent_tool_calls_use_distinct_session_stores(tmp_path) -> None:
    parent = _context(tmp_path, "session_parent")
    child = _context(tmp_path, "session_child")

    def executor_for(ctx):
        agent = SimpleNamespace(
            tracker_info=None,
            tools=SimpleNamespace(get=lambda _name: object()),
            run_context=ctx,
        )
        executor = ToolExecutor(agent)

        async def observe(*_args, **_kwargs):
            await asyncio.sleep(0)
            return {
                "session_id": get_execution_context().session_id,
                "store": get_backup_store(),
            }

        executor._execute_single_tool_inner = AsyncMock(side_effect=observe)
        return executor

    pc = {"tool_name": "observe", "arguments": {}}
    with services_scope():
        parent_result, child_result = await asyncio.gather(
            executor_for(parent).execute_single_tool(pc, None, None),
            executor_for(child).execute_single_tool(pc, None, None),
        )

    assert parent_result == {
        "session_id": "session_parent",
        "store": parent.checkpoint_store,
    }
    assert child_result == {
        "session_id": "session_child",
        "store": child.checkpoint_store,
    }


@pytest.mark.asyncio
async def test_run_context_propagates_to_backup_work_in_thread(tmp_path) -> None:
    ctx = _context(tmp_path, "session_thread")
    with services_scope(), execution_context_scope(run_context=ctx):
        observed = await asyncio.to_thread(lambda: get_services().backup_store)
    assert observed is ctx.checkpoint_store


def test_resumed_session_reopens_same_recovery_ledger(tmp_path) -> None:
    original = FileBackupStore(
        session_id="session_resume",
        backup_root=str(tmp_path / "backups"),
    )
    target = tmp_path / "target.txt"
    target.write_text("before")
    original.backup_file(str(target), "modify")

    resumed = FileBackupStore(
        session_id="session_resume",
        backup_root=str(tmp_path / "backups"),
    )
    other = FileBackupStore(
        session_id="session_other",
        backup_root=str(tmp_path / "backups"),
    )

    assert resumed.backup_dir == original.backup_dir
    assert len(resumed.get_history()) == 1
    assert other.get_history() == []


@pytest.mark.parametrize("session_id", ["", ".", "..", "../escape", "a/b"])
def test_session_store_rejects_unsafe_identifiers(tmp_path, session_id: str) -> None:
    with pytest.raises(ValueError, match="path-safe"):
        FileBackupStore(session_id=session_id, backup_root=str(tmp_path))


def test_default_store_does_not_follow_ambient_history_session(tmp_path, monkeypatch) -> None:
    from coderAI.system.history import history_manager

    monkeypatch.setattr(history_manager, "current_session", SimpleNamespace(session_id="wrong"))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    store = FileBackupStore()

    assert store.session_id is None
    assert store.backup_dir == (tmp_path / ".coderAI" / "backups" / "global").resolve()
