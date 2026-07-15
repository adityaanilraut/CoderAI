"""Phase 1.4 — git tools must not clobber files via `--output=`.

``git_diff``/``git_show``/``git_blame`` appended an LLM-controlled
ref/file_path straight onto the argv with no ``--`` separator, so
``git diff --output=/tmp/x`` / ``git show --output=/tmp/x`` would create or
truncate an arbitrary file. These tests prove the injection is refused and no
file is written, while normal path/ref usage still works.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coderAI.tools.git import GitDiffTool
from coderAI.tools.git_extended import GitBlameTool, GitShowTool


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A real git repo with one committed + modified file."""
    env = {
        **{"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    }

    def run(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**__import__("os").environ, **env},
        )

    run("init")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "Test")
    f = tmp_path / "real.txt"
    f.write_text("line one\nline two\n")
    run("add", "real.txt")
    run("commit", "-m", "initial")
    f.write_text("line one\nline two changed\nline three\n")
    return tmp_path


# ── Injection is refused and writes nothing ──────────────────────────────────


def test_git_diff_output_injection_creates_no_file(tmp_path: Path, git_repo: Path) -> None:
    target = tmp_path / "pwned_diff"
    result = asyncio.run(
        GitDiffTool().execute(repo_path=str(git_repo), file_path=f"--output={target}")
    )
    assert result["success"] is False
    assert not target.exists(), "git diff --output= clobbered a file"


def test_git_show_output_injection_creates_no_file(tmp_path: Path, git_repo: Path) -> None:
    target = tmp_path / "pwned_show"
    result = asyncio.run(GitShowTool().execute(repo_path=str(git_repo), ref=f"--output={target}"))
    assert result["success"] is False
    assert not target.exists(), "git show --output= clobbered a file"


def test_git_blame_output_injection_creates_no_file(tmp_path: Path, git_repo: Path) -> None:
    target = tmp_path / "pwned_blame"
    result = asyncio.run(
        GitBlameTool().execute(repo_path=str(git_repo), file_path=f"--output={target}")
    )
    assert result["success"] is False
    assert not target.exists(), "git blame --output= clobbered a file"


# ── Normal usage still works with the `--` separator in place ────────────────


def test_git_diff_normal_path_still_works(git_repo: Path) -> None:
    result = asyncio.run(GitDiffTool().execute(repo_path=str(git_repo), file_path="real.txt"))
    assert result["success"] is True, result
    assert result["has_diff"] is True
    assert "line three" in result["diff"]


def test_git_show_normal_ref_still_works(git_repo: Path) -> None:
    result = asyncio.run(GitShowTool().execute(repo_path=str(git_repo), ref="HEAD"))
    assert result["success"] is True, result


def test_git_blame_normal_path_still_works(git_repo: Path) -> None:
    result = asyncio.run(GitBlameTool().execute(repo_path=str(git_repo), file_path="real.txt"))
    assert result["success"] is True, result
