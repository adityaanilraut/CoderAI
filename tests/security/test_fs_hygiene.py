"""Phase 8 — filesystem & secret-at-rest hygiene.

Threat: an untrusted repo / MCP / web page coaxes the agent into (a) tampering
with a persistence file or credential store at rest (``~/.bashrc``, ``~/.netrc``,
``~/.coderAI``), (b) reading/writing through a swapped symlink to escape the
project scope, or (c) leaving conversation content / backups world-readable on a
shared host.

Covers: the protected-dotfile denylist (enforced even with the project-scope
opt-out), owner-only backups + preserved restore mode, owner-only session history
and index, the config-dir re-chmod, and the ReadFileTool symlink re-guard.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from coderAI.tools.filesystem.manage import MoveFileTool
from coderAI.tools.filesystem.read_write import ReadFileTool, WriteFileTool

posix_only = pytest.mark.skipif(
    os.name == "nt", reason="POSIX permission bits are a no-op on Windows"
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ══════════════════════════════════════════════════════════════════════════
# Protected-dotfile denylist — refused even with the scope opt-out on
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "rel",
    [
        ".bashrc",
        ".zshrc",
        ".profile",
        ".bash_profile",
        ".gitconfig",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".ssh/authorized_keys",
        ".config/gcloud/credentials.db",
        ".coderAI/mcp_credentials.json",
    ],
)
async def test_protected_dotfile_write_refused_even_with_opt_out(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, rel: str
) -> None:
    # The sandbox opt-out must NOT be a way around the protected-path denylist.
    monkeypatch.setenv("CODERAI_ALLOW_OUTSIDE_PROJECT", "1")
    target = isolated_home / rel
    res = await WriteFileTool().execute(path=str(target), content="pwned")
    assert not res["success"]
    assert "protected" in res["error"].lower()
    assert not target.exists()


async def test_protected_path_move_destination_refused(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODERAI_ALLOW_OUTSIDE_PROJECT", "1")
    src = tmp_path / "evil_rc"
    src.write_text("export EVIL=1\n")
    res = await MoveFileTool().execute(source=str(src), destination=str(isolated_home / ".bashrc"))
    assert not res["success"]
    assert "protected" in res["error"].lower()
    assert not (isolated_home / ".bashrc").exists()


# ══════════════════════════════════════════════════════════════════════════
# Backups: owner-only, in an owner-only dir, with preserved restore mode
# ══════════════════════════════════════════════════════════════════════════


@posix_only
def test_backups_are_owner_only(isolated_home: Path, tmp_path: Path) -> None:
    from coderAI.tools.undo import FileBackupStore

    store = FileBackupStore(backup_dir=str(tmp_path / "bk"))
    src = tmp_path / "secret.txt"
    src.write_text("token=abc")
    src.chmod(0o644)  # world-readable source

    entry = store.backup_file(str(src), "modify")

    backup = Path(entry["backup_path"])
    assert _mode(backup) == 0o600  # backup tightened at rest
    assert _mode(store.backup_dir) == 0o700  # dir tightened at rest


@posix_only
def test_restore_preserves_original_mode(tmp_path: Path) -> None:
    from coderAI.tools.undo import FileBackupStore

    store = FileBackupStore(backup_dir=str(tmp_path / "bk"))
    src = tmp_path / "run.sh"
    src.write_text("#!/bin/sh\necho hi\n")
    src.chmod(0o755)  # executable

    store.backup_file(str(src), "modify")
    src.write_text("clobbered")
    src.chmod(0o600)

    result = store.undo_last()
    assert result["success"], result
    # The +x mode is restored, not left at the backup's 0600.
    assert _mode(src) == 0o755


# ══════════════════════════════════════════════════════════════════════════
# Session history + index are owner-only (no world-readable temp window)
# ══════════════════════════════════════════════════════════════════════════


@posix_only
def test_session_and_index_files_are_owner_only(isolated_home: Path) -> None:
    from coderAI.system.history import HistoryManager

    hm = HistoryManager()
    hm.save_session_data(
        {
            "session_id": "session_1700000000_abcd1234",
            "messages": [{"role": "user", "content": "hello"}],
            "created_at": 1700000000,
            "updated_at": 1700000000,
            "model": "claude-opus-4-8",
        }
    )
    session_file = hm.history_dir / "session_1700000000_abcd1234.json"
    index_file = hm.history_dir / "index.json"
    assert session_file.exists()
    assert _mode(session_file) == 0o600
    assert index_file.exists()
    assert _mode(index_file) == 0o600


# ══════════════════════════════════════════════════════════════════════════
# Config dir is re-chmod'd to 0700 even when it already existed loosely
# ══════════════════════════════════════════════════════════════════════════


@posix_only
def test_config_dir_rechmod_on_construct(isolated_home: Path) -> None:
    from coderAI.system.config import ConfigManager

    dot = isolated_home / ".coderAI"
    dot.chmod(0o755)  # simulate a pre-existing world-readable config dir
    assert _mode(dot) == 0o755

    ConfigManager()  # __init__ must re-restrict it

    assert _mode(dot) == 0o700


# ══════════════════════════════════════════════════════════════════════════
# ReadFileTool refuses a symlink leaf (parity with the write path)
# ══════════════════════════════════════════════════════════════════════════


@posix_only
async def test_read_refuses_symlink_leaf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODERAI_ALLOW_OUTSIDE_PROJECT", "1")
    secret = tmp_path / "outside_secret"
    secret.write_text("SECRET")
    link = tmp_path / "innocent.txt"
    link.symlink_to(secret)

    res = await ReadFileTool().execute(path=str(link))
    assert not res["success"]
    assert "symlink" in res["error"].lower()
