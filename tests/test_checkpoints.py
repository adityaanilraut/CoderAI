"""Tests for conversation rewind / checkpoint + restore (roadmap #3).

Covers the three layers added for ``/rewind``:
  - ``Session`` checkpoint capture + truncation (``coderAI/system/history.py``)
  - ``FileBackupStore.restore_after`` file reversion (``coderAI/tools/undo.py``)
  - ``Agent.rewind_to`` orchestration + the ``rewind`` bridge command
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from coderAI.bridge.controller import _cmd_rewind
from coderAI.core.agent import Agent
from coderAI.system.history import Checkpoint, Session
from coderAI.tools.undo import FileBackupStore


# ── Session-level checkpoints ──────────────────────────────────────────


def _two_turn_session() -> Session:
    s = Session(session_id="session_1_abcd1234")
    s.add_message("system", "sys")
    s.add_checkpoint("first")
    s.add_message("user", "first")
    s.add_message("assistant", "reply 1")
    s.add_checkpoint("second")
    s.add_message("user", "second")
    s.add_message("assistant", "reply 2")
    return s


def test_add_checkpoint_numbers_turns_and_indices() -> None:
    s = Session(session_id="session_1_abcd1234")
    s.add_message("system", "sys")
    c1 = s.add_checkpoint("a")
    s.add_message("user", "a")
    s.add_message("assistant", "ra")
    c2 = s.add_checkpoint("b")

    assert (c1.turn, c2.turn) == (1, 2)
    assert c1.message_index == 1  # only the system message precedes turn 1
    assert c2.message_index == 3  # system + user + assistant precede turn 2


def test_truncate_to_checkpoint_drops_turn_and_tail() -> None:
    s = _two_turn_session()
    target = s.truncate_to_checkpoint(2)

    assert isinstance(target, Checkpoint) and target.turn == 2
    # Back to before turn 2: system + user1 + assistant1.
    assert [m.role for m in s.messages] == ["system", "user", "assistant"]
    assert s.messages[1].content == "first"
    # Checkpoint 2 (and any later) is gone; checkpoint 1 survives.
    assert [c.turn for c in s.checkpoints] == [1]


def test_truncate_to_first_turn_resets_to_system_prompt() -> None:
    s = _two_turn_session()
    s.truncate_to_checkpoint(1)
    assert [m.role for m in s.messages] == ["system"]
    assert s.checkpoints == []


def test_truncate_invalid_turn_returns_none_and_no_mutation() -> None:
    s = _two_turn_session()
    before = len(s.messages)
    assert s.truncate_to_checkpoint(99) is None
    assert len(s.messages) == before
    assert len(s.checkpoints) == 2


def test_checkpoints_round_trip_through_json() -> None:
    s = _two_turn_session()
    restored = Session(**s.model_dump())
    assert [c.label for c in restored.checkpoints] == ["first", "second"]
    assert restored.checkpoints[1].message_index == 3


def test_legacy_session_without_checkpoints_defaults_empty() -> None:
    # A session JSON written before this feature has no "checkpoints" key.
    s = Session(session_id="session_1_abcd1234", messages=[{"role": "system", "content": "x"}])
    assert s.checkpoints == []


# ── FileBackupStore.restore_after ──────────────────────────────────────


def _set_ts(entry: dict, dt: datetime) -> None:
    entry["timestamp"] = dt.isoformat()


def test_restore_after_reverts_modify_newer_than_cutoff(tmp_path) -> None:
    store = FileBackupStore(backup_dir=str(tmp_path / "backups"))
    f = tmp_path / "a.txt"
    f.write_text("original")

    store.backup_file(str(f), "modify")  # entry 0 saves "original"
    f.write_text("v1")
    store.backup_file(str(f), "modify")  # entry 1 saves "v1"
    f.write_text("v2")

    # Stamp entry 0 before the cutoff, entry 1 after it.
    _set_ts(store.index[0], datetime(2020, 1, 1, 0, 0, 0))
    _set_ts(store.index[1], datetime(2020, 1, 1, 0, 0, 2))
    store._save_index()
    cutoff = datetime(2020, 1, 1, 0, 0, 1).timestamp()

    result = store.restore_after(cutoff)

    assert result["success"] is True
    assert result["count"] == 1
    assert f.read_text() == "v1"  # reverted one step, not all the way to "original"
    # The consumed (newer) entry is dropped; the older one remains.
    assert len(store.index) == 1


def test_restore_after_deletes_files_created_since_cutoff(tmp_path) -> None:
    store = FileBackupStore(backup_dir=str(tmp_path / "backups"))
    new_file = tmp_path / "created.txt"

    store.backup_file(str(new_file), "create")  # records the file did not exist
    new_file.write_text("brand new")
    _set_ts(store.index[0], datetime(2020, 1, 1, 0, 0, 5))
    store._save_index()

    result = store.restore_after(datetime(2020, 1, 1, 0, 0, 1).timestamp())

    assert str(new_file) in result["deleted"]
    assert not new_file.exists()


def test_restore_after_ignores_entries_at_or_before_cutoff(tmp_path) -> None:
    store = FileBackupStore(backup_dir=str(tmp_path / "backups"))
    f = tmp_path / "a.txt"
    f.write_text("original")
    store.backup_file(str(f), "modify")
    f.write_text("changed")
    _set_ts(store.index[0], datetime(2020, 1, 1, 0, 0, 0))
    store._save_index()

    # Cutoff is after the only backup → nothing to revert.
    result = store.restore_after(datetime(2020, 1, 1, 0, 0, 5).timestamp())

    assert result["count"] == 0
    assert f.read_text() == "changed"
    assert len(store.index) == 1


def test_restore_after_reports_missing_backup(tmp_path) -> None:
    store = FileBackupStore(backup_dir=str(tmp_path / "backups"))
    f = tmp_path / "a.txt"
    f.write_text("original")
    store.backup_file(str(f), "modify")
    # Delete the backup file out from under the index entry.
    backup_path = store.index[0]["backup_path"]
    import os

    os.unlink(backup_path)
    _set_ts(store.index[0], datetime(2020, 1, 1, 0, 0, 5))
    store._save_index()

    result = store.restore_after(datetime(2020, 1, 1, 0, 0, 1).timestamp())

    assert result["restored"] == []
    assert any("backup missing" in e for e in result["errors"])


# ── Agent.rewind_to (called unbound against a lightweight stub) ─────────


def _agent_stub(session):
    return SimpleNamespace(session=session, save_session=MagicMock())


def test_agent_rewind_to_conversation_only() -> None:
    s = _two_turn_session()
    stub = _agent_stub(s)

    result = Agent.rewind_to(stub, 2, restore_files=False)

    assert result["ok"] is True
    assert result["turn"] == 2
    assert result["dropped_turns"] == 1
    assert result["restored_files"] == []
    assert [m.role for m in s.messages] == ["system", "user", "assistant"]
    stub.save_session.assert_called_once()


def test_agent_rewind_to_restores_files(monkeypatch) -> None:
    s = _two_turn_session()
    stub = _agent_stub(s)
    fake_store = SimpleNamespace(
        restore_after=MagicMock(
            return_value={"restored": ["/x/a.txt"], "deleted": [], "errors": []}
        )
    )
    monkeypatch.setattr("coderAI.tools.undo.get_backup_store", lambda: fake_store)

    # Capture the cutoff before the rewind drops checkpoint 1.
    cutoff = s.checkpoints[0].created_at

    result = Agent.rewind_to(stub, 1, restore_files=True)

    assert result["ok"] is True
    assert result["restored_files"] == ["/x/a.txt"]
    # The cutoff passed is the matched checkpoint's created_at.
    fake_store.restore_after.assert_called_once_with(cutoff)


def test_agent_rewind_to_invalid_turn_does_not_save() -> None:
    s = _two_turn_session()
    stub = _agent_stub(s)

    result = Agent.rewind_to(stub, 99, restore_files=False)

    assert result["ok"] is False
    assert "No checkpoint" in result["error"]
    stub.save_session.assert_not_called()
    assert len(s.messages) == 5  # untouched


def test_agent_rewind_to_no_session() -> None:
    stub = SimpleNamespace(session=None, save_session=MagicMock())
    result = Agent.rewind_to(stub, 1)
    assert result["ok"] is False
    stub.save_session.assert_not_called()


# ── _cmd_rewind bridge handler ─────────────────────────────────────────


def _rewind_server(rewind_return):
    agent = SimpleNamespace(rewind_to=MagicMock(return_value=rewind_return))
    return SimpleNamespace(
        agent=agent,
        _turn_lock=asyncio.Lock(),
        emit=MagicMock(),
        emit_status=MagicMock(),
    )


@pytest.mark.asyncio
async def test_cmd_rewind_success_with_files() -> None:
    server = _rewind_server(
        {
            "ok": True,
            "turn": 1,
            "label": "first",
            "dropped_turns": 1,
            "restored_files": ["/x/a.txt"],
            "file_errors": [],
        }
    )
    await _cmd_rewind(server, {"turn": 1, "files": True})

    server.agent.rewind_to.assert_called_once_with(1, restore_files=True)
    levels = [c.args[0] for c in server.emit.call_args_list]
    assert "success" in levels
    server.emit_status.assert_called_once()


@pytest.mark.asyncio
async def test_cmd_rewind_non_integer_turn_warns() -> None:
    server = _rewind_server({"ok": True})
    await _cmd_rewind(server, {"turn": "abc"})

    server.agent.rewind_to.assert_not_called()
    assert server.emit.call_args_list[0].args[0] == "warning"
    server.emit_status.assert_not_called()


@pytest.mark.asyncio
async def test_cmd_rewind_backend_not_ok_warns() -> None:
    server = _rewind_server({"ok": False, "error": "No checkpoint for turn 9."})
    await _cmd_rewind(server, {"turn": 9})

    levels = [c.args[0] for c in server.emit.call_args_list]
    assert levels == ["warning"]
    server.emit_status.assert_not_called()


@pytest.mark.asyncio
async def test_cmd_rewind_surfaces_file_errors() -> None:
    server = _rewind_server(
        {
            "ok": True,
            "turn": 1,
            "label": "x",
            "dropped_turns": 1,
            "restored_files": [],
            "file_errors": ["a.txt: backup missing"],
        }
    )
    await _cmd_rewind(server, {"turn": 1, "files": True})

    levels = [c.args[0] for c in server.emit.call_args_list]
    assert "success" in levels and "warning" in levels
    server.emit_status.assert_called_once()
