"""Security regressions for isolated mutating-subagent patch integration."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from coderAI.core.delegation_worktree import DelegationWorktree, DelegationWorktreeError


pytestmark = pytest.mark.security


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "CoderAI Tests")
    (root / "safe.txt").write_text("safe\n")
    _git(root, "add", "safe.txt")
    _git(root, "commit", "-qm", "base")
    return root


def test_patch_cannot_follow_parent_directory_symlink(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    root = _repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "target.txt").write_text("outside\n")
    manager = DelegationWorktree(
        parent_root=root,
        storage_root=tmp_path / "worktrees",
        worktree_id="security_parent_symlink",
    )
    child = manager.create()
    try:
        nested = child / "nested"
        nested.mkdir()
        (nested / "target.txt").write_text("child\n")
        prepared = manager.prepare_patch()
        os.symlink(outside, root / "nested")

        with pytest.raises(DelegationWorktreeError, match="symlink"):
            manager.integrate(prepared)
        assert (outside / "target.txt").read_text() == "outside\n"
    finally:
        manager.cleanup()


def test_reviewed_patch_cannot_be_swapped_before_integration(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manager = DelegationWorktree(
        parent_root=root,
        storage_root=tmp_path / "worktrees",
        worktree_id="security_review_swap",
    )
    child = manager.create()
    try:
        (child / "safe.txt").write_text("reviewed\n")
        prepared = manager.prepare_patch()
        (child / "safe.txt").write_text("unreviewed payload\n")

        with pytest.raises(DelegationWorktreeError, match="changed after patch review"):
            manager.integrate(prepared)
        assert (root / "safe.txt").read_text() == "safe\n"
    finally:
        manager.cleanup()
