"""Tests for UndoTool, UndoHistoryTool, and FileBackupStore."""

import asyncio
import pytest

from coderAI.tools.undo import FileBackupStore, UndoTool, UndoHistoryTool


@pytest.fixture
def backup_store(tmp_path):
    """Return a fresh FileBackupStore backed by a temp directory."""
    return FileBackupStore(backup_dir=str(tmp_path / "backups"))


@pytest.fixture
def sample_file(tmp_path):
    f = tmp_path / "sample.txt"
    f.write_text("original content")
    return f


class TestFileBackupStore:
    def test_backup_existing_file(self, backup_store, sample_file):
        entry = backup_store.backup_file(str(sample_file))
        assert "backup_path" in entry
        assert entry["operation"] == "modify"
        assert len(backup_store.index) == 1

    def test_backup_nonexistent_file_returns_error(self, backup_store, tmp_path):
        result = backup_store.backup_file(str(tmp_path / "nope.txt"))
        assert "error" in result

    def test_backup_create_operation(self, backup_store, tmp_path):
        new_file = tmp_path / "newfile.txt"
        entry = backup_store.backup_file(str(new_file), operation="create")
        assert entry["operation"] == "create"
        assert entry["backup_path"] is None

    def test_undo_last_restores_file(self, backup_store, sample_file):
        backup_store.backup_file(str(sample_file))
        sample_file.write_text("modified content")

        result = backup_store.undo_last()
        assert result["success"]
        assert sample_file.read_text() == "original content"

    def test_undo_last_empty_returns_error(self, backup_store):
        result = backup_store.undo_last()
        assert not result["success"]

    def test_undo_create_deletes_file(self, backup_store, tmp_path):
        new_file = tmp_path / "created.txt"
        backup_store.backup_file(str(new_file), operation="create")
        new_file.write_text("new file content")

        result = backup_store.undo_last()
        assert result["success"]
        assert not new_file.exists()

    def test_undo_specific_valid_index(self, backup_store, sample_file):
        backup_store.backup_file(str(sample_file))
        sample_file.write_text("changed")
        result = backup_store.undo_specific(0)
        assert result["success"]
        assert sample_file.read_text() == "original content"

    def test_undo_specific_out_of_range(self, backup_store):
        result = backup_store.undo_specific(99)
        assert not result["success"]

    def test_get_history_returns_recent_first(self, backup_store, sample_file):
        backup_store.backup_file(str(sample_file), operation="modify")
        backup_store.backup_file(str(sample_file), operation="delete")
        history = backup_store.get_history(limit=2)
        assert len(history) == 2
        assert history[0]["operation"] == "delete"

    def test_max_backups_per_file_enforced(self, backup_store, sample_file):
        from coderAI.tools.undo import MAX_BACKUPS_PER_FILE
        for _ in range(MAX_BACKUPS_PER_FILE + 3):
            backup_store.backup_file(str(sample_file))
        file_entries = [e for e in backup_store.index if e["filepath"] == str(sample_file.resolve())]
        assert len(file_entries) <= MAX_BACKUPS_PER_FILE


class TestUndoTool:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path, monkeypatch):
        self.store = FileBackupStore(backup_dir=str(tmp_path / "backups"))
        monkeypatch.setattr("coderAI.tools.undo.get_backup_store", lambda: self.store)
        self.tool = UndoTool()

    def test_undo_with_no_history(self):
        result = asyncio.run(self.tool.execute())
        assert not result["success"]

    def test_undo_last(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("old")
        self.store.backup_file(str(f))
        f.write_text("new")
        result = asyncio.run(self.tool.execute())
        assert result["success"]
        assert f.read_text() == "old"

    def test_undo_specific_index(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("old")
        self.store.backup_file(str(f))
        f.write_text("new")
        result = asyncio.run(self.tool.execute(index=0))
        assert result["success"]


class TestUndoHistoryTool:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path, monkeypatch):
        self.store = FileBackupStore(backup_dir=str(tmp_path / "backups"))
        monkeypatch.setattr("coderAI.tools.undo.get_backup_store", lambda: self.store)
        self.tool = UndoHistoryTool()

    def test_empty_history(self):
        result = asyncio.run(self.tool.execute())
        assert result["success"]
        assert result["count"] == 0

    def test_history_after_backup(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("x")
        self.store.backup_file(str(f))
        result = asyncio.run(self.tool.execute(limit=5))
        assert result["success"]
        assert result["count"] == 1

    def test_limit_respected(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("x")
        for _ in range(5):
            self.store.backup_file(str(f))
        result = asyncio.run(self.tool.execute(limit=2))
        assert result["count"] <= 2
