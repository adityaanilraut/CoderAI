"""Session metadata and persisted transcript export coverage."""

import json

from click.testing import CliRunner

from coderAI.cli import history_cmd
from coderAI.system.history import HistoryManager


def _manager(tmp_path):
    manager = HistoryManager()
    manager.history_dir = tmp_path / "history"
    manager.history_dir.mkdir(parents=True, exist_ok=True)
    return manager


def test_session_rename_tags_persist_in_index_and_filter(tmp_path):
    manager = _manager(tmp_path)
    session = manager.create_session(model="test-model")
    session.add_message("user", "audit this")
    manager.save_session(session)

    assert manager.rename_session(session.session_id, "Release audit")
    assert manager.tag_session(session.session_id, ["CI", "release", "ci"])

    loaded = manager.load_session(session.session_id)
    assert loaded is not None
    assert loaded.name == "Release audit"
    assert loaded.tags == ["CI", "release"]
    assert manager.list_sessions(tag="ci")[0]["name"] == "Release audit"
    assert manager.list_sessions(query="release")[0]["session_id"] == session.session_id

    index = json.loads((manager.history_dir / "index.json").read_text(encoding="utf-8"))
    assert index[session.session_id]["name"] == "Release audit"
    assert index[session.session_id]["tags"] == ["CI", "release"]


def test_history_rename_and_tag_commands(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    session = manager.create_session(model="test-model")
    manager.save_session(session)
    monkeypatch.setattr(history_cmd, "history_manager", manager)
    runner = CliRunner()

    renamed = runner.invoke(history_cmd.history, ["rename", session.session_id, "Named session"])
    tagged = runner.invoke(history_cmd.history, ["tag", session.session_id, "audit", "ci"])
    removed = runner.invoke(history_cmd.history, ["tag", "--remove", session.session_id, "audit"])

    assert renamed.exit_code == 0
    assert tagged.exit_code == 0
    assert removed.exit_code == 0
    loaded = manager.load_session(session.session_id)
    assert loaded is not None
    assert loaded.name == "Named session"
    assert loaded.tags == ["ci"]


def test_history_export_uses_complete_persisted_transcript(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    session = manager.create_session(model="test-model")
    session.name = "Long audit"
    session.tags = ["export"]
    for index in range(205):
        session.add_message("user", f"message-{index}")
    session.add_message("assistant", "finished", reasoning_content="private reasoning")
    manager.save_session(session)
    monkeypatch.setattr(history_cmd, "history_manager", manager)
    runner = CliRunner()

    json_result = runner.invoke(
        history_cmd.history, ["export", session.session_id, "--format", "json"]
    )
    markdown_result = runner.invoke(
        history_cmd.history, ["export", session.session_id, "--format", "markdown"]
    )

    assert json_result.exit_code == 0
    exported = json.loads(json_result.stdout)
    assert len(exported["messages"]) == 206
    assert exported["messages"][-1]["reasoning_content"] == "private reasoning"
    assert markdown_result.exit_code == 0
    assert "# Long audit" in markdown_result.stdout
    assert "message-204" in markdown_result.stdout
    assert "private reasoning" in markdown_result.stdout
