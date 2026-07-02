"""Phase 1.1 — every model-driven subprocess runs with a scrubbed environment.

``system/proc.run_scrubbed`` drops credential-bearing env vars before spawning,
so a model-authored ``run_command`` / ``git`` / ``package_manager`` / ``run_tests``
child can never read ``$ANTHROPIC_API_KEY`` (or any secret-looking var) out of
the inherited environment. Benign vars survive so real builds/tests keep working.

Covers:
* ``run_scrubbed`` end-to-end against the real interpreter: secrets dropped,
  benign vars kept, return contract ``(rc, stdout, stderr, timed_out)`` honoured.
* the terminal ``run_command`` tool executed for real prints ``None`` for the
  secret it would otherwise inherit.
* ``git`` and ``package_manager`` route through ``run_scrubbed`` — asserted at
  the real spawn boundary via an ``env``-capturing spy (independent of whether
  the external binary is installed).
"""

from __future__ import annotations

import asyncio
import shlex
import sys

import pytest

from coderAI.system import proc
from coderAI.system.proc import run_scrubbed
from coderAI.tools.git import _run_git_command
from coderAI.tools.package_manager import PackageManagerTool
from coderAI.tools.terminal import RunCommandTool


# ── run_scrubbed: real subprocess, real environment ──────────────────────────


def test_run_scrubbed_drops_secrets_keeps_benign(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
    monkeypatch.setenv("SCRUB_BENIGN_MARKER", "keepme")

    code = (
        "import os, sys; "
        "sys.stdout.write(str(os.environ.get('ANTHROPIC_API_KEY'))); "
        "sys.stdout.write('|'); "
        "sys.stdout.write(str(os.environ.get('SCRUB_BENIGN_MARKER')))"
    )
    rc, out, err, timed_out = asyncio.run(run_scrubbed([sys.executable, "-c", code], timeout=30))

    assert timed_out is False
    assert rc == 0, err.decode(errors="replace")
    # Secret scrubbed → ``None``; a benign var still passes through.
    assert out.decode() == "None|keepme"


# ── terminal run_command: executed for real ──────────────────────────────────


def test_run_command_env_has_no_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")

    code = "import os, sys; sys.stdout.write(str(os.environ.get('ANTHROPIC_API_KEY')))"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
    result = asyncio.run(RunCommandTool().execute(command=command))

    assert result["success"] is True, result
    assert result["stdout"].strip() == "None"


# ── env-capture spy at the real spawn boundary ───────────────────────────────


class _FakeProc:
    """Stand-in for the spawned child: no output, clean exit."""

    def __init__(self) -> None:
        self.returncode = 0
        self.pid = 4242

    async def communicate(self, input: bytes | None = None):  # noqa: A002 - matches asyncio API
        return b"", b""

    async def wait(self) -> int:
        return 0


@pytest.fixture
def spawn_spy(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Capture the ``env`` / ``argv`` handed to ``create_subprocess_exec``.

    Patches the symbol ``run_scrubbed`` actually calls, so the recorded ``env``
    is the exact (already-scrubbed) mapping the child would have received.
    """
    captured: dict = {}

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        return _FakeProc()

    monkeypatch.setattr(proc.asyncio, "create_subprocess_exec", fake_exec)
    return captured


def test_git_command_env_has_no_secrets(
    monkeypatch: pytest.MonkeyPatch, spawn_spy: dict, tmp_path
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
    monkeypatch.setenv("GIT_BENIGN_MARKER", "keepme")

    result = asyncio.run(_run_git_command(["status"], str(tmp_path), validate_scope=False))

    assert result["success"] is True
    assert spawn_spy["argv"][0] == "git"
    env = spawn_spy["env"]
    assert env is not None
    assert "ANTHROPIC_API_KEY" not in env
    # git legitimately needs the rest of the environment (HOME/PATH/GIT_*).
    assert env.get("GIT_BENIGN_MARKER") == "keepme"


def test_package_manager_env_has_no_secrets(
    monkeypatch: pytest.MonkeyPatch, spawn_spy: dict
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
    monkeypatch.setenv("NPM_TOKEN", "npm-should-not-leak")
    monkeypatch.setenv("PKG_BENIGN_MARKER", "keepme")

    # Pretend the manager binary is on PATH so we reach the spawn boundary.
    monkeypatch.setattr(
        "coderAI.tools.package_manager.shutil.which", lambda name: f"/usr/bin/{name}"
    )
    result = asyncio.run(PackageManagerTool().execute(action="list", manager="pip"))

    assert result["success"] is True, result
    env = spawn_spy["env"]
    assert env is not None
    assert "ANTHROPIC_API_KEY" not in env
    assert "NPM_TOKEN" not in env
    assert env.get("PKG_BENIGN_MARKER") == "keepme"
