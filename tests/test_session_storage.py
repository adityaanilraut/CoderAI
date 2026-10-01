"""Session replacement contracts: exact bytes, file modes and orphan recovery."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from coderai.soul.session.store import JsonlSessionStore


def _replacement(store, kind):
    if kind == "index":
        index = {"entries": [], "originalPath": "é", "version": 1}
        return store.index_path, lambda: store.save_index(index), json.dumps(index, indent=2)
    if kind == "rows":
        rows = [{"role": "user", "content": "é\nnext"}, {"type": "stop", "ok": False}]
        return (
            store.messages_path("s"),
            lambda: store.replace_rows("s", rows),
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        )
    lines = ['{"content":"é"}', "", '{"done":true}']
    return (
        store.messages_path("s"),
        lambda: store.write_raw_lines("s", lines),
        "".join(line + "\n" for line in lines),
    )


@pytest.mark.parametrize("kind", ["index", "rows", "raw"])
def test_replacement_preserves_serialization_and_umask(tmp_path, kind):
    store = JsonlSessionStore(str(tmp_path))
    target, write, content = _replacement(store, kind)
    target.write_text("old", encoding="utf-8")
    target.chmod(0o640)
    previous = os.umask(0o077)
    try:
        write()
    finally:
        os.umask(previous)
    assert target.read_bytes() == content.replace("\n", os.linesep).encode("utf-8")
    # Session replacements historically create a fresh inode using the process umask.
    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    else:
        # Windows exposes the writable bit rather than POSIX umask permissions.
        assert target.stat().st_mode & stat.S_IWRITE
    assert not list(store.project_dir.glob("*.tmp-*"))


@pytest.mark.parametrize("kind", ["index", "rows", "raw"])
def test_failed_replace_keeps_old_target_and_recoverable_orphan(tmp_path, monkeypatch, kind):
    store = JsonlSessionStore(str(tmp_path))
    target, write, content = _replacement(store, kind)
    target.write_text("old", encoding="utf-8")

    def fail_replace(path, destination):
        assert Path(destination) == target
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        write()
    assert target.read_text() == "old"
    orphans = list(store.project_dir.glob("*.tmp-*"))
    assert len(orphans) == 1
    assert orphans[0].read_text(encoding="utf-8") == content
    assert store.cleanup_orphan_tmps() == 1
    assert target.read_text() == "old"
