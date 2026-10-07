"""Offline migration preflight, collision preservation and crash recovery."""

from __future__ import annotations

import json

import pytest

from coderai.cli.migrate import migrate_sessions, run_migrate


def legacy(tmp_path):
    source = tmp_path / "legacy"
    source.mkdir()
    (source / "sessions-index.json").write_text(
        json.dumps({"version": 1, "entries": [{"id": "old", "summary": "original"}]})
    )
    (source / "old.jsonl").write_text('{"role":"user","content":"preserve"}\n')
    (source / "old").mkdir()
    (source / "old" / "state.json").write_text('{"plan_mode":true}')
    (source / "images").mkdir()
    (source / "images" / "image.bin").write_bytes(b"\x00\xff")
    return source, tmp_path / "workspace"


def test_dry_run_is_read_only(tmp_path):
    source, workspace = legacy(tmp_path)
    report = migrate_sessions(source, workspace, dry_run=True)
    assert report["sessions"]["imported"] == ["old"]
    assert len(report["files"]["copied"]) == 3
    assert not workspace.exists()


def test_migration_copies_state_assets_and_is_idempotent(tmp_path):
    source, workspace = legacy(tmp_path)
    source_bytes = (source / "old.jsonl").read_bytes()
    first = migrate_sessions(source, workspace)
    destination = workspace / ".coderai" / "sessions"
    assert first["sessions"]["imported"] == ["old"]
    assert (destination / "old.jsonl").read_bytes() == source_bytes
    assert (destination / "images" / "image.bin").read_bytes() == b"\x00\xff"
    assert (destination / "old" / "state.json").read_bytes() == (
        source / "old" / "state.json"
    ).read_bytes()
    assert (source / "old.jsonl").read_bytes() == source_bytes
    second = migrate_sessions(source, workspace)
    assert second["files"]["copied"] == []
    assert second["sessions"]["imported"] == []
    assert second["sessions"]["existing"] == ["old"]
    assert not (destination / ".migration.lock").exists()
    assert len(json.loads((destination / "sessions-index.json").read_text())["entries"]) == 1


def test_existing_log_collision_keeps_target_and_index(tmp_path):
    source, workspace = legacy(tmp_path)
    destination = workspace / ".coderai" / "sessions"
    destination.mkdir(parents=True)
    index = {"version": 1, "entries": [{"id": "old", "summary": "newer"}]}
    (destination / "sessions-index.json").write_text(json.dumps(index))
    (destination / "old.jsonl").write_text("target data")
    report = migrate_sessions(source, workspace)
    assert report["sessions"]["conflicts"] == ["old"]
    assert (destination / "old.jsonl").read_text() == "target data"
    assert not (destination / "old" / "state.json").exists()
    assert (
        json.loads((destination / "sessions-index.json").read_text())["entries"] == index["entries"]
    )


def test_copy_failure_leaves_index_uncommitted_and_releases_lock(tmp_path, monkeypatch):
    import coderai.cli.migrate as module

    source, workspace = legacy(tmp_path)
    destination = workspace / ".coderai" / "sessions"

    def failed(*args):
        raise OSError("copy failed")

    monkeypatch.setattr(module, "_copy_new_file", failed)
    with pytest.raises(OSError, match="copy failed"):
        migrate_sessions(source, workspace)
    assert not (destination / "sessions-index.json").exists()
    assert not (destination / ".migration.lock").exists()
    assert (source / "old.jsonl").exists()


def test_dry_run_reports_symlink_conflicts_without_reading_targets(tmp_path):
    source, workspace = legacy(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("private")
    (source / "linked.jsonl").symlink_to(outside)
    report = migrate_sessions(source, workspace, dry_run=True)
    assert "linked.jsonl" in report["files"]["conflicts"]
    assert not workspace.exists()


def test_migration_refuses_overlapping_roots_and_lock(tmp_path):
    source, workspace = legacy(tmp_path)
    with pytest.raises(ValueError, match="overlap"):
        migrate_sessions(source, source / "nested")
    destination = workspace / ".coderai" / "sessions"
    destination.mkdir(parents=True)
    (destination / ".migration.lock").write_text("123")
    with pytest.raises(ValueError, match="holds"):
        migrate_sessions(source, workspace)
    assert (destination / ".migration.lock").read_text() == "123"


def test_cli_report_and_malformed_source_are_explicit(tmp_path, capsys):
    source, workspace = legacy(tmp_path)
    report_path = tmp_path / "report.json"
    assert (
        run_migrate(
            [
                "--source",
                str(source),
                "--work-dir",
                str(workspace),
                "--dry-run",
                "--report",
                str(report_path),
            ]
        )
        == 0
    )
    assert json.loads(report_path.read_text())["dryRun"] is True
    assert not workspace.exists()
    (source / "sessions-index.json").write_text('{"entries":[{"id":"../bad"}]}')
    assert run_migrate(["--source", str(source), "--work-dir", str(workspace)]) == 1
    assert "Invalid session id" in capsys.readouterr().err


def test_automatic_migration_falls_back_without_publishing_partial_index(tmp_path, monkeypatch):
    import shutil
    import coderai.cli.migrate as module
    from coderai.soul.session.store import JsonlSessionStore, get_project_code

    source, workspace = legacy(tmp_path)
    share = tmp_path / "share"
    global_store = share / "projects" / get_project_code(str(workspace))
    shutil.copytree(source, global_store)
    monkeypatch.setenv("CODERAI_SHARE_DIR", str(share))

    def failed(*args):
        raise OSError("copy failed")

    monkeypatch.setattr(module, "_copy_new_file", failed)
    store = JsonlSessionStore(str(workspace))
    assert store.project_dir == global_store
    local = workspace / ".coderai" / "sessions"
    assert not (local / "sessions-index.json").exists()
    assert not (local / ".migration.lock").exists()
    assert (global_store / "old.jsonl").exists()


def test_migrate_dispatch_bypasses_interactive_parser(cli_isolated_home, monkeypatch):
    import coderai.cli.migrate as module
    import coderai.ui.shell.app as app

    calls = []

    def fake_run(argv):
        calls.append(argv)
        return 37

    def no_interactive_parser():
        raise AssertionError("migration must use its own offline parser")

    monkeypatch.setattr(module, "run_migrate", fake_run)
    monkeypatch.setattr(app, "_build_parser", no_interactive_parser)
    assert app.main(["migrate", "--dry-run"]) == 37
    assert calls == [["--dry-run"]]
