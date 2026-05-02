"""Regression tests for the symlink-leaf TOCTOU guard (M-14, M-15).

The filesystem tools' protection checks call ``Path.resolve()`` which follows
symlinks. A symlink that *currently* points at a benign in-project file would
pass the check; if swapped between the check and the actual open/move/delete,
the operation would land on whatever the link now targets. The mitigation is
to refuse symlink leaves outright across the mutating filesystem tools.

These tests skip on platforms where ``os.symlink`` is not available (Windows
without admin / dev mode).
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

from coderAI.tools.filesystem import (
    ApplyDiffTool,
    CopyFileTool,
    DeleteFileTool,
    MoveFileTool,
    SearchReplaceTool,
    WriteFileTool,
)


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Symlinks require elevated privileges on Windows; the guard is "
    "still wired up there but not exercised by this test.",
)


def _allow_outside_project(monkeypatch):
    """Tools enforce project-scope by default. The tmp_path tests live outside
    the project root, so opt-out via the documented env var to isolate the
    symlink check we actually want to assert on."""
    monkeypatch.setenv("CODERAI_ALLOW_OUTSIDE_PROJECT", "1")


@pytest.fixture
def real_and_link(tmp_path):
    """A real file in tmp_path plus a symlink alongside it."""
    real = tmp_path / "real.txt"
    real.write_text("real contents\n")
    link = tmp_path / "link.txt"
    os.symlink(real, link)
    return real, link


def test_apply_diff_refuses_symlink_leaf(real_and_link, monkeypatch):
    _allow_outside_project(monkeypatch)
    _, link = real_and_link
    tool = ApplyDiffTool()
    result = asyncio.run(
        tool.execute(
            path=str(link),
            diff="@@ -1,1 +1,1 @@\n-real contents\n+swapped\n",
        )
    )
    assert result["success"] is False
    assert result.get("error_code") == "symlink"


def test_search_replace_refuses_symlink_leaf(real_and_link, monkeypatch):
    _allow_outside_project(monkeypatch)
    real, link = real_and_link
    tool = SearchReplaceTool()
    result = asyncio.run(
        tool.execute(path=str(link), search="real", replace="bogus")
    )
    assert result["success"] is False
    assert result.get("error_code") == "symlink"
    # The real file's contents must remain untouched.
    assert real.read_text() == "real contents\n"


def test_write_file_refuses_existing_symlink_leaf(real_and_link, monkeypatch):
    _allow_outside_project(monkeypatch)
    real, link = real_and_link
    tool = WriteFileTool()
    result = asyncio.run(tool.execute(path=str(link), content="overwritten"))
    assert result["success"] is False
    assert result.get("error_code") == "symlink"
    # The real file's contents must remain untouched.
    assert real.read_text() == "real contents\n"


def test_delete_file_refuses_symlink_leaf(real_and_link, monkeypatch):
    _allow_outside_project(monkeypatch)
    real, link = real_and_link
    tool = DeleteFileTool()
    result = asyncio.run(tool.execute(path=str(link)))
    assert result["success"] is False
    assert result.get("error_code") == "symlink"
    # Both the link and the real file must still exist.
    assert link.is_symlink()
    assert real.exists()


def test_move_file_refuses_symlink_source(real_and_link, tmp_path, monkeypatch):
    _allow_outside_project(monkeypatch)
    _, link = real_and_link
    dst = tmp_path / "moved.txt"
    tool = MoveFileTool()
    result = asyncio.run(tool.execute(source=str(link), destination=str(dst)))
    assert result["success"] is False
    assert result.get("error_code") == "symlink"
    assert not dst.exists()


def test_copy_file_refuses_symlink_source(real_and_link, tmp_path, monkeypatch):
    """Without the guard, ``shutil.copy2`` would follow the link and copy
    the *target's* contents — a swapped link could hand us /etc/passwd."""
    _allow_outside_project(monkeypatch)
    _, link = real_and_link
    dst = tmp_path / "copied.txt"
    tool = CopyFileTool()
    result = asyncio.run(tool.execute(source=str(link), destination=str(dst)))
    assert result["success"] is False
    assert result.get("error_code") == "symlink"
    assert not dst.exists()


def test_real_files_still_work(tmp_path, monkeypatch):
    """The guard must not regress the legitimate path: writes to a real file
    still succeed, including overwrites and reads."""
    _allow_outside_project(monkeypatch)
    f = tmp_path / "plain.txt"

    write = WriteFileTool()
    result = asyncio.run(write.execute(path=str(f), content="hello"))
    assert result["success"] is True
    assert f.read_text() == "hello"

    sr = SearchReplaceTool()
    result = asyncio.run(sr.execute(path=str(f), search="hello", replace="world"))
    assert result["success"] is True
    assert f.read_text() == "world"
