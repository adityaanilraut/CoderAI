"""Phase 1.2 / 1.5 — code-execution sandboxing.

Covers:
* ``scrub_env`` drops credential-bearing env vars but keeps benign ones.
* ``python_repl`` runs with a scrubbed environment (a secret in the parent's
  env is *not* visible to the model's code).
* A timed-out ``python_repl`` / ``run_command`` kills its whole process group,
  so a backgrounded grandchild is reaped instead of orphaned.
* The repl clamps a nonsensical timeout instead of failing.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

from coderAI.system.proc import is_secret_env_var, scrub_env
from coderAI.tools.repl import PythonREPLTool
from coderAI.tools.terminal import RunCommandTool

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX process-group semantics")


# ── scrub_env ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "MY_SERVICE_SECRET",
        "DB_PASSWORD",
        "SOME_PRIVATE_KEY",
        "NPM_TOKEN",
    ],
)
def test_is_secret_env_var_flags_credentials(name: str) -> None:
    assert is_secret_env_var(name) is True


@pytest.mark.parametrize("name", ["PATH", "HOME", "LANG", "PWD", "TERM", "VIRTUAL_ENV"])
def test_is_secret_env_var_keeps_benign(name: str) -> None:
    assert is_secret_env_var(name) is False


def test_scrub_env_removes_secrets_keeps_rest() -> None:
    base = {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "OPENAI_API_KEY": "sk-secret",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "GITHUB_TOKEN": "ghp_secret",
        "MY_APP_CONFIG": "keepme",
    }
    scrubbed = scrub_env(base)
    assert scrubbed["PATH"] == "/usr/bin"
    assert scrubbed["HOME"] == "/home/x"
    assert scrubbed["MY_APP_CONFIG"] == "keepme"
    assert "OPENAI_API_KEY" not in scrubbed
    assert "AWS_SECRET_ACCESS_KEY" not in scrubbed
    assert "GITHUB_TOKEN" not in scrubbed


# ── python_repl runs with a scrubbed environment ─────────────────────────────


def test_python_repl_env_has_no_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-should-not-leak")
    monkeypatch.setenv("REPL_BENIGN_MARKER", "visible-ok")

    code = "import os, json; print(json.dumps(sorted(os.environ)))"
    result = asyncio.run(PythonREPLTool().execute(code=code))

    assert result["success"] is True, result
    seen = set(json.loads(result["stdout"]))
    assert "OPENAI_API_KEY" not in seen
    assert "AWS_SECRET_ACCESS_KEY" not in seen
    # A non-secret var is still passed through so real scripts keep working.
    assert "REPL_BENIGN_MARKER" in seen


# ── timeout clamp ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad_timeout", [0, -5])
def test_python_repl_clamps_nonpositive_timeout(bad_timeout: int) -> None:
    result = asyncio.run(PythonREPLTool().execute(code="print('clamp-ok')", timeout=bad_timeout))
    assert result["success"] is True, result
    assert "clamp-ok" in result["stdout"]


# ── process-group reaping on timeout ─────────────────────────────────────────


@POSIX_ONLY
def test_python_repl_timeout_reaps_grandchild(tmp_path) -> None:
    marker = tmp_path / "leaked.txt"
    grandchild = "import time; time.sleep(2); open(%r, 'w').write('leaked')" % str(marker)
    code = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', %r])\n"
        "time.sleep(30)\n"
    ) % grandchild

    result = asyncio.run(PythonREPLTool().execute(code=code, timeout=1))
    assert result["success"] is False
    assert result.get("error_code") == "timeout"

    # If only the direct child were killed, the backgrounded grandchild would
    # survive and write the marker at ~2s. Wait past that and assert it didn't.
    asyncio.run(asyncio.sleep(2.5))
    assert not marker.exists(), "grandchild orphaned — process group was not killed"


@POSIX_ONLY
def test_run_command_timeout_reaps_grandchild(tmp_path) -> None:
    marker = tmp_path / "leaked_cmd.txt"
    # Parent hangs on `sleep 30`; a backgrounded grandchild would touch the
    # marker at ~2s unless the whole group is killed on timeout.
    command = f"(sleep 2 && touch {marker}) & sleep 30"
    result = asyncio.run(RunCommandTool().execute(command=command, timeout=1))
    assert result["success"] is False
    assert result.get("error_code") == "timeout"

    asyncio.run(asyncio.sleep(2.5))
    assert not marker.exists(), "grandchild orphaned — process group was not killed"
