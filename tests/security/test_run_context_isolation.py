"""Security boundaries for session-owned recovery state."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from coderAI.core.execution_context import create_run_context, execution_context_scope
from coderAI.core.services import services_scope
from coderAI.system.history import history_manager
from coderAI.tools.undo import FileBackupStore, get_backup_store


pytestmark = pytest.mark.security


def _bound_context(tmp_path, session_id: str):
    store = FileBackupStore(
        session_id=session_id,
        backup_root=str(tmp_path / "backups"),
    )
    return replace(
        create_run_context(workspace_root=str(tmp_path)),
        session_id=session_id,
        checkpoint_store=store,
    )


def test_ambient_history_cannot_redirect_active_recovery_store(tmp_path, monkeypatch) -> None:
    parent = _bound_context(tmp_path, "session_parent")
    monkeypatch.setattr(history_manager, "current_session", SimpleNamespace(session_id="child"))

    with services_scope(), execution_context_scope(run_context=parent):
        assert get_backup_store() is parent.checkpoint_store


def test_child_scope_cannot_inherit_parent_recovery_store(tmp_path) -> None:
    parent = _bound_context(tmp_path, "session_parent")
    child = _bound_context(tmp_path, "session_child")

    with services_scope(), execution_context_scope(run_context=parent):
        assert get_backup_store() is parent.checkpoint_store
        with execution_context_scope(run_context=child):
            assert get_backup_store() is child.checkpoint_store
        assert get_backup_store() is parent.checkpoint_store


def test_recovery_store_rejects_path_escape_identifier(tmp_path) -> None:
    with pytest.raises(ValueError, match="path-safe"):
        FileBackupStore(session_id="../../escape", backup_root=str(tmp_path))
    assert not (tmp_path.parent / "escape").exists()


def test_recovery_store_rejects_preexisting_symlink_escape(tmp_path) -> None:
    root = tmp_path / "backups"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "session_escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes backup_root"):
        FileBackupStore(session_id="session_escape", backup_root=str(root))
