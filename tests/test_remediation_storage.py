"""Owned persistence and restore must reject invalid paths before side effects."""

from __future__ import annotations

import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from coderai.subagents.builder import setup_subagent_scratchpad, cleanup_subagent_scratchpad
from coderai.subagents.store import SubagentStore
from coderai.soul.session.store import JsonlSessionStore
from coderai.utils.common.file_history import GitFileHistory
from coderai.utils.storage import write_bytes


@pytest.mark.parametrize("identifier", ["", "../outside", "/outside", "a/b", "a\\b", "..", 1])
def test_store_ids_fail_before_creating_files(tmp_path, identifier):
    store = SubagentStore(tmp_path / "session")
    session = JsonlSessionStore(str(tmp_path))
    with pytest.raises(ValueError):
        store.create_instance(agent_id=identifier, subagent_type="coder", description="test")
    with pytest.raises(ValueError):
        session.append_row(identifier, {"seq": 0})
    assert not store.root.exists()


def test_subagent_links_and_corrupt_shape_do_not_escape(tmp_path):
    store = SubagentStore(tmp_path / "session")
    store.root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (store.root / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        store.delete_instance("linked")
    assert outside.is_dir()
    store.create_instance(agent_id="broken", subagent_type="coder", description="test")
    store.meta_path("broken").write_text("[]")
    assert store.get_instance("broken") is None
    assert store.list_instances() == []


def test_owned_scratch_cleanup_preserves_supplied_workspaces(tmp_path):
    user_workspace = tmp_path / "attempt-project"
    user_workspace.mkdir()
    (user_workspace / "keep").write_text("keep")
    cleanup_subagent_scratchpad(str(user_workspace))
    assert (user_workspace / "keep").read_text() == "keep"
    scratch = Path(setup_subagent_scratchpad(str(tmp_path), "session"))
    (scratch / "work").write_text("work")
    cleanup_subagent_scratchpad(str(scratch))
    assert not scratch.exists()
    with pytest.raises(ValueError):
        setup_subagent_scratchpad(str(tmp_path), "../../outside")


def test_atomic_storage_does_not_follow_parent_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        write_bytes(tmp_path / "link" / "escape", b"secret")
    assert not (outside / "escape").exists()


def test_instance_updates_serialize_read_modify_write(tmp_path):
    store = SubagentStore(tmp_path / "session")
    store.create_instance(agent_id="agent", subagent_type="coder", description="test")
    with ThreadPoolExecutor(2) as pool:
        status = pool.submit(store.update_instance, "agent", status="completed")
        task = pool.submit(store.update_instance, "agent", last_task_id="last")
        status.result()
        task.result()
    record = store.require_instance("agent")
    assert record.status == "completed" and record.last_task_id == "last"


def test_file_history_preflights_all_paths_and_session_ancestry(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    history = GitFileHistory(str(project), str(tmp_path / "history"))
    file = project / "file"
    file.write_text("old")
    checkpoint = history.record_checkpoint("session", [str(file)], "old").checkpoint_hash
    file.write_text("current")
    history.record_checkpoint("session", [str(file)], "current")
    outside = tmp_path / "outside"
    outside.write_text("keep")
    with pytest.raises(ValueError):
        history.record_checkpoint("session", [str(outside)], "outside")
    file.unlink()
    file.symlink_to(outside)
    with pytest.raises(ValueError):
        history.restore("session", checkpoint)
    assert outside.read_text() == "keep"
    file.unlink()
    file.write_text("current")
    history.ensure_session("other")
    with pytest.raises(RuntimeError):
        history.restore("other", checkpoint)
    assert file.read_text() == "current"
    history.restore("session", checkpoint)
    assert file.read_text() == "old"


def test_repair_rows_are_structured_failures(tmp_path):
    store = JsonlSessionStore(str(tmp_path))
    store.replace_rows(
        "session",
        [
            {
                "id": "a",
                "role": "assistant",
                "tool_calls": [{"id": "call", "function": {"name": "read", "arguments": "{}"}}],
            },
            {"id": "u", "role": "user", "content": "next"},
        ],
    )
    store.validate_and_repair_invariants("session")
    result = next(row for row in store.read_rows("session") if row.get("role") == "tool")
    assert json.loads(result["content"])["ok"] is False


def test_wire_append_refuses_leaf_link(tmp_path):
    from coderai.wire.file import WireFile
    from coderai.wire.types import TextPart

    outside = tmp_path / "outside"
    outside.write_text("keep")
    path = tmp_path / "wire.jsonl"
    wire = WireFile(path)
    path.symlink_to(outside)
    with pytest.raises((OSError, ValueError)):
        wire.append_message_sync(TextPart("escape"))
    assert outside.read_text() == "keep"
