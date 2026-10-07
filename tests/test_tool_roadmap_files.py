"""File safety regressions covering hidden content, stale reads, and undo."""

import os

import pytest

from coderai.file_snippets import get_file_state
from coderai.tools.file.read import handle_read_tool
from coderai.tools.file.replace import handle_str_replace_editor_tool
from coderai.tools.file.write import handle_write_tool
from coderai.tools.legacy.observation import FileObservationTracker
from coderai.tools.legacy.types import ToolExecutionContext


def test_long_lines_and_redaction_never_authorize_full_overwrite(tmp_path):
    context = ToolExecutionContext("hidden", str(tmp_path))
    for i, content in enumerate(["x" * 2500, "sk-" + "X" * 30]):
        path = tmp_path / f"file-{i}"
        path.write_text(content)
        read = handle_read_tool({"file_path": str(path)}, context)
        assert read.ok
        assert get_file_state(context.session_id, str(path)).is_partial_view
        result = handle_write_tool({"file_path": str(path), "content": "replacement"}, context)
        assert not result.ok
        assert path.read_text() == content


def test_timestamp_preservation_does_not_bypass_stale_detection(tmp_path):
    path = tmp_path / "code.py"
    path.write_text("original")
    tracker = FileObservationTracker()
    tracker.record_observation("s", str(path))
    before = path.stat()
    path.write_text("modified")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert not tracker.check_mutation_allowed("s", str(path))[0]


def test_observation_failure_denies_mutation(tmp_path, monkeypatch):
    path = tmp_path / "file"
    path.write_text("text")
    tracker = FileObservationTracker()
    tracker.record_observation("s", str(path))
    monkeypatch.setattr(
        "coderai.tools.legacy.observation.file_digest",
        lambda p: (_ for _ in ()).throw(PermissionError("unreadable")),
    )
    allowed, error = tracker.check_mutation_allowed("s", str(path))
    assert not allowed and "VERIFICATION_FAILED" in error


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be"])
def test_undo_preview_and_failure_preserve_history_and_encoding(tmp_path, monkeypatch, encoding):
    path = tmp_path / "windows.txt"
    original = "\ufeffone\r\ntwo\r\n".encode(encoding)
    path.write_bytes(original)
    context = ToolExecutionContext("undo", str(tmp_path))
    read = handle_read_tool({"file_path": str(path)}, context)
    assert read.ok and "two" in read.output and not read.metadata.get("isBinary")
    assert handle_str_replace_editor_tool({"command": "view", "path": str(path)}, context).ok
    edited = handle_str_replace_editor_tool(
        {"command": "str_replace", "path": str(path), "old_str": "two", "new_str": "three"}, context
    )
    assert edited.ok, edited.error
    changed = path.read_bytes()
    context.dry_run = True
    assert handle_str_replace_editor_tool({"command": "undo_edit", "path": str(path)}, context).ok
    assert path.read_bytes() == changed
    context.dry_run = False
    with monkeypatch.context() as patch:
        patch.setattr(
            "coderai.tools.file.replace.write_file_with_callbacks",
            lambda *a: (_ for _ in ()).throw(OSError("disk full")),
        )
        failed = handle_str_replace_editor_tool(
            {"command": "undo_edit", "path": str(path)}, context
        )
        assert not failed.ok
    restored = handle_str_replace_editor_tool({"command": "undo_edit", "path": str(path)}, context)
    assert restored.ok, restored.error
    assert path.read_bytes() == original


def test_large_reads_remain_bounded_without_full_file_loading(tmp_path, monkeypatch):
    path = tmp_path / "large.txt"
    with path.open("wb") as stream:
        for _ in range(100):
            stream.write(b"a" * 100_000 + b"\n")
    monkeypatch.setattr(
        "pathlib.Path.read_bytes",
        lambda p: (_ for _ in ()).throw(AssertionError("whole-file read")),
    )
    context = ToolExecutionContext("large", str(tmp_path))
    result = handle_read_tool({"file_path": str(path), "offset": 40, "limit": 3}, context)
    assert result.ok, result.error
    assert len(result.output.encode()) < 50 * 1024
    assert get_file_state("large", str(path)).raw_digest


def test_todos_keep_ids_cancelled_and_full_titles(tmp_path):
    from types import SimpleNamespace
    from coderai.session_state import (
        SessionState,
        TodoItemState,
        save_session_state,
        load_session_state,
    )
    from coderai.tools.todo import handle_todo_write_tool

    state = SessionState(todos=[TodoItemState(title="old", status="done")])
    assert state.todos[0].status == "completed"
    manager = SimpleNamespace(
        get_session_state=lambda sid: state,
        _save_session_state=lambda sid: save_session_state(state, tmp_path),
    )
    context = ToolExecutionContext("todos", str(tmp_path), session_manager=manager)
    result = handle_todo_write_tool(
        {"todos": [{"id": "keep-id", "content": "x" * 600, "status": "cancelled"}]}, context
    )
    assert result.ok
    restored = load_session_state(tmp_path).todos[0]
    assert (
        restored.id == "keep-id" and restored.status == "cancelled" and len(restored.title) == 600
    )
