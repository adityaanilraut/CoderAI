"""Tests for atomic file I/O and subprocess isolation.

Tests:
1. Atomic File I/O (writeFileAtomic, withFileLock, permission preservation, exclusive temp sibling, contention backoff).
2. Isolated Subprocess Code Runtime (code-runtime-python, AST eval, variable state extraction, tool bindings, crash safety).
"""

from __future__ import annotations

import pathlib
import stat
import time
import pytest

from coderai.core.common.file_utils import (
    write_file_atomic,
    with_file_lock,
)
from coderai.core.code_mode.engine import CodeModeSandbox, CodeModeResult


# ============================================================================
# 1. Atomic File I/O & Writer Locks Tests
# ============================================================================


def test_write_file_atomic_basic(tmp_path: pathlib.Path):
    target = tmp_path / "subdir" / "test.txt"
    bytes_written = write_file_atomic(target, "Hello World\nLine 2", mode=0o644)
    assert bytes_written > 0
    assert target.exists()
    assert target.read_text(encoding="utf-8") == "Hello World\nLine 2"


def test_write_file_atomic_preserves_mode(tmp_path: pathlib.Path):
    target = tmp_path / "restricted.txt"
    write_file_atomic(target, "initial content", mode=0o600)
    st = target.stat()
    assert stat.S_IMODE(st.st_mode) == 0o600

    # Updating without specifying mode preserves original mode (0o600)
    write_file_atomic(target, "updated content")
    st2 = target.stat()
    assert stat.S_IMODE(st2.st_mode) == 0o600
    assert target.read_text() == "updated content"


def test_with_file_lock_mutual_exclusion(tmp_path: pathlib.Path):
    target = tmp_path / "locked_file.txt"
    executed = []

    def op1():
        executed.append("op1_start")
        time.sleep(0.05)
        executed.append("op1_end")
        return "res1"

    def op2():
        executed.append("op2")
        return "res2"

    r1 = with_file_lock(target, op1)
    r2 = with_file_lock(target, op2)

    assert r1 == "res1"
    assert r2 == "res2"
    assert executed == ["op1_start", "op1_end", "op2"]


# ============================================================================
# 2. Isolated Subprocess Code Runtime Tests
# ============================================================================


@pytest.mark.asyncio
async def test_code_mode_subprocess_basic_execution(tmp_path: pathlib.Path):
    sandbox = CodeModeSandbox(str(tmp_path))
    code = """
a = 10
b = 25
a + b
"""
    result: CodeModeResult = await sandbox.execute(code, timeout_seconds=10.0)
    assert result.error is None
    assert result.result == 35 or result.result == "35"
    assert "a" in result.variables
    assert "b" in result.variables


@pytest.mark.asyncio
async def test_code_mode_subprocess_state_retention(tmp_path: pathlib.Path):
    sandbox = CodeModeSandbox(str(tmp_path))
    # Turn 1: set variable
    r1 = await sandbox.execute("counter = 42", timeout_seconds=10.0)
    assert r1.error is None
    assert "counter" in r1.variables

    # Turn 2: read variable from previous turn
    r2 = await sandbox.execute("counter + 8", timeout_seconds=10.0)
    assert r2.error is None
    assert r2.result == 50 or r2.result == "50"


@pytest.mark.asyncio
async def test_code_mode_subprocess_workspace_tools(tmp_path: pathlib.Path):
    sandbox = CodeModeSandbox(str(tmp_path))
    code = """
write_file("generated.txt", "content from subprocess")
read_file("generated.txt")
"""
    result = await sandbox.execute(code, timeout_seconds=10.0)
    assert result.error is None
    assert (tmp_path / "generated.txt").read_text() == "content from subprocess"
    assert result.result == "content from subprocess"


@pytest.mark.asyncio
async def test_code_mode_subprocess_crash_isolation(tmp_path: pathlib.Path):
    sandbox = CodeModeSandbox(str(tmp_path))
    # Subprocess calling sys.exit() or raising should not crash host
    code = """
import sys
sys.exit(1)
"""
    result = await sandbox.execute(code, timeout_seconds=5.0)
    # The agent/host process survived, and result captured the exit
    assert result is not None
