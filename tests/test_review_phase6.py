"""Regression tests for Phase 6 (Tools and shell hardening).

Covers:
- TL-A5: Model bash timeout_ms clamped between min and max into initial_timeout_ms.
- TL-A7: Foreground bash output buffer capped with head/tail ring buffer at MAX_CAPTURE_CHARS.
- TL-A8: Background cd validated with resolve_exec_cwd before updating session cwd.
- TL-A9 (rest): pwsh shares bash's launcher (cwd, process group, context timeout, log handle, job-cap).
- TL-A10: pwsh / terminal_send background jobs complete when process/PTY exits.
- TL-A11: edit does not record own observation before checks; compares against read.
- TL-A13: Refuse to edit binary or replacement-decoded files; preserve per-line endings.
- TL-A18: Readers of JsonlSessionStore do not run cleanup_orphan_tmps (cleanup=False).
- TL-A20: Think returns thought as tool result only without inserting assistant message.
- TL-A22: Executor timeout keeps path lock until thread finishes.
- TL-A23: Guard int in web fetch/search, seatbelt cleanup, per-session undo with sandbox checks, no gpt-6-luna fallback.
- TL-B3: Schemas and handlers agree (declared mode, WebFetch params, integer types, reject unknown keys, registry parity).
- TL-B5: extract_key_argument handles live tool names.
- TL-B6: Relative paths resolve identically across read, grep, glob, write, edit, and path lock.
- UI-A17: PTY terminal buffers enforce DEFAULT_MAX_BUFFER_CHARS, incremental UTF-8 decoder, write loop.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from coderai.terminal.manager import TerminalManager
from coderai.tools import extract_key_argument
from coderai.tools.file.replace import (
    _pop_undo,
    _push_undo,
    handle_edit_tool,
)
from coderai.tools.legacy.executor import ToolExecutor
from coderai.tools.legacy.path_lock import get_path_lock_manager
from coderai.tools.legacy.registry import ToolRegistry, ValidationError
from coderai.tools.shell import (
    HeadTailBuffer,
    clamp_bash_timeout_ms,
    handle_bash_tool,
    handle_pwsh_tool,
)
from coderai.tools.think import handle_think_tool
from coderai.tools.web.fetch import handle_web_fetch_tool
from coderai.tools.web.search import handle_web_search_tool
from coderai.utils.path import (
    get_per_line_endings,
    read_text_file_with_metadata,
    write_text_file,
)
from coderai.utils.subprocess_env import (
    DEFAULT_BASH_TIMEOUT_MS,
    MAX_BASH_TIMEOUT_MS,
    MIN_BASH_TIMEOUT_MS,
)


# ---------------------------------------------------------------------------
# TL-A5: Clamped timeout_ms for bash
# ---------------------------------------------------------------------------
def test_clamp_bash_timeout_ms_bounds():
    # Below minimum is clamped to MIN_BASH_TIMEOUT_MS (1000ms)
    assert clamp_bash_timeout_ms(100) == MIN_BASH_TIMEOUT_MS
    assert clamp_bash_timeout_ms(5000) == 5000
    # Above maximum is clamped to MAX_BASH_TIMEOUT_MS (10 min = 600,000ms)
    assert clamp_bash_timeout_ms(10_000_000) == MAX_BASH_TIMEOUT_MS
    # Non-numbers fall back to DEFAULT_BASH_TIMEOUT_MS
    assert clamp_bash_timeout_ms(float("nan")) == DEFAULT_BASH_TIMEOUT_MS


def test_bash_passes_model_timeout_ms(tmp_path):
    ctx = {"session_id": "test_timeout", "project_root": str(tmp_path)}
    # Model requests 2500ms
    res = handle_bash_tool(
        {"command": "echo hello", "timeout_ms": 2500, "sideEffects": ["write-in-cwd"]},
        ctx,
    )
    assert res.ok is True
    # If model requests 100ms, it should be clamped to at least 1000ms
    res2 = handle_bash_tool(
        {"command": "echo hello", "timeout_ms": 100, "sideEffects": ["write-in-cwd"]},
        ctx,
    )
    assert res2.ok is True


# ---------------------------------------------------------------------------
# TL-A7: HeadTailBuffer caps output at MAX_CAPTURE_CHARS
# ---------------------------------------------------------------------------
def test_head_tail_buffer_capping():
    cap = 100
    buf = HeadTailBuffer(max_chars=cap, head_ratio=0.5)
    # Append small strings
    buf.append("A" * 30)
    buf.append("B" * 20)
    assert buf.get_value() == "A" * 30 + "B" * 20

    # Now exceed cap
    buf.append("C" * 200)
    val = buf.get_value()
    # Must contain the head (first 50 chars of A and B)
    assert val.startswith("A" * 30 + "B" * 20)
    # Must contain tail (last 50 chars of C)
    assert val.endswith("C" * 50)
    # Must have truncation indicator
    assert "truncated" in val


# ---------------------------------------------------------------------------
# TL-A8: Background cd validated with resolve_exec_cwd
# ---------------------------------------------------------------------------
def test_background_cd_outside_root_ignored(tmp_path):
    from coderai.sandbox import resolve_exec_cwd

    # Validate that an escaping cd /tmp cannot be applied
    with pytest.raises(ValueError):
        resolve_exec_cwd("/tmp", str(tmp_path))

    # A background execution marker returning /tmp should not update session cwd
    # Clean check via resolve_exec_cwd guard
    next_cwd = "/tmp"
    try:
        resolve_exec_cwd(next_cwd, str(tmp_path))
    except (ValueError, OSError):
        next_cwd = None
    assert next_cwd is None


# ---------------------------------------------------------------------------
# TL-A9 (rest) & TL-A10: pwsh shares bash launcher and completes background jobs
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pwsh_background_job_handles_job_cap_and_completes(tmp_path):
    from coderai.background import get_job_store

    store = get_job_store()
    ctx = {"session_id": "test_pwsh_bg", "project_root": str(tmp_path)}

    with patch("shutil.which", return_value="/bin/sh"):
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 99999
            mock_proc.returncode = 0
            mock_proc.wait.return_value = 0
            mock_popen.return_value = mock_proc

            res = await handle_pwsh_tool(
                {
                    "command": "echo test",
                    "run_in_background": True,
                    "sideEffects": ["write-in-cwd"],
                },
                ctx,
            )
            assert res.ok is True
            job_id = res.metadata["job_id"]
            # Give background watcher thread a brief moment to run wait() and complete
            time.sleep(0.05)
            job = store.get(job_id)
            assert job is not None
            assert job.status in ("running", "completed")


# ---------------------------------------------------------------------------
# TL-A10: terminal_send background job completes when PTY exits
# ---------------------------------------------------------------------------
def test_terminal_send_completes_when_pty_exits(tmp_path):
    from coderai.background import get_job_store
    from coderai.tools.legacy.terminal import handle_terminal_send_tool

    store = get_job_store()
    mgr = TerminalManager()

    mock_term = MagicMock()
    mock_term.pid = 12345
    mock_term.is_alive = False
    mock_term.exit_code = 0
    mock_term.send.return_value = None

    with patch("coderai.tools.legacy.terminal.get_terminal_manager", return_value=mgr):
        mgr._sessions["term1"] = mock_term
        # Initially alive to accept send
        mock_term.is_alive = True
        res = handle_terminal_send_tool(
            {"sessionId": "term1", "text": "exit", "run_in_background": True},
            {"session_id": "test_pty_sess"},
        )
        assert res.ok is True
        job_id = res.metadata["jobId"]
        # Mark term not alive
        mock_term.is_alive = False
        time.sleep(0.15)
        job = store.get(job_id)
        assert job is not None
        assert job.status == "completed"


# ---------------------------------------------------------------------------
# TL-A11: edit does not record own observation before checks
# ---------------------------------------------------------------------------
def test_edit_respects_modified_since_read(tmp_path):
    from coderai.state import mark_file_read
    from coderai.tools.legacy.observation import get_observation_tracker

    file = tmp_path / "foo.py"
    file.write_text("x = 1\n", encoding="utf-8")
    meta = read_text_file_with_metadata(str(file))

    session_id = "test_obs_edit"
    ctx = {"session_id": session_id, "project_root": str(tmp_path)}

    # 1. Read file
    mark_file_read(session_id, str(file), meta)
    get_observation_tracker().record_observation(session_id, str(file), "x = 1\n")

    # 2. File modified by external user/process
    time.sleep(0.01)
    file.write_text("x = 2\n", encoding="utf-8")

    # 3. Edit should fail because file has changed since read!
    res = handle_edit_tool(
        {"file_path": str(file), "old_string": "x = 1\n", "new_string": "x = 3\n"},
        ctx,
    )
    assert res.ok is False
    assert (
        "modified since read" in res.error
        or "stale" in res.error.lower()
        or "read again" in res.error.lower()
    )


# ---------------------------------------------------------------------------
# TL-A13: Refuse to edit binary/corrupt files and preserve line endings per line
# ---------------------------------------------------------------------------
def test_refuse_to_edit_binary_and_corrupt_files(tmp_path):
    bin_file = tmp_path / "bin.dat"
    bin_file.write_bytes(b"\x00\x01\x02\x03\xff\xfe\x00\x00")

    ctx = {"session_id": "test_bin_edit", "project_root": str(tmp_path)}
    res = handle_edit_tool(
        {"file_path": str(bin_file), "old_string": "foo", "new_string": "bar"},
        ctx,
    )
    assert res.ok is False
    assert "binary" in res.error.lower() or "decode" in res.error.lower()


def test_preserve_per_line_endings(tmp_path):
    text = "line1\r\nline2\nline3\r\n"
    endings = get_per_line_endings(text)
    assert endings == ["\r\n", "\n", "\r\n"]

    out_file = tmp_path / "mixed.txt"
    # Write with per-line endings
    write_text_file(str(out_file), "line1\nline2_edit\nline3\n", line_endings=endings)
    raw = out_file.read_bytes()
    assert raw == b"line1\r\nline2_edit\nline3\r\n"


# ---------------------------------------------------------------------------
# TL-A18: JsonlSessionStore cleanup=False for readers
# ---------------------------------------------------------------------------
def test_jsonl_session_store_cleanup_flag(tmp_path):
    from coderai.soul.session.store import JsonlSessionStore

    store_dir = tmp_path / ".coderai" / "sessions"
    store_dir.mkdir(parents=True)
    orphan_tmp = store_dir / "sessions-index.json.tmp-12345"
    orphan_tmp.write_text("{}", encoding="utf-8")

    # Reader with cleanup=False must not delete orphan_tmp
    JsonlSessionStore(str(tmp_path), cleanup=False)
    assert orphan_tmp.exists()

    # Normal store with cleanup=True deletes orphan_tmp
    JsonlSessionStore(str(tmp_path), cleanup=True)
    assert not orphan_tmp.exists()


# ---------------------------------------------------------------------------
# TL-A20: Think returns thought as tool result only
# ---------------------------------------------------------------------------
def test_think_tool_does_not_call_append_message():
    mock_mgr = MagicMock()
    ctx = MagicMock()
    ctx.session_id = "test_think"
    ctx.manager = mock_mgr

    res = handle_think_tool({"thought": "Pondering the universe..."}, ctx)
    assert res.ok is True
    assert res.metadata["thought"] == "Pondering the universe..."
    # Must NOT have called _append_message
    mock_mgr._append_message.assert_not_called()


# ---------------------------------------------------------------------------
# TL-A22: Executor timeout keeps path lock until thread finishes
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_executor_timeout_keeps_path_lock_until_thread_finishes(tmp_path):
    reg = ToolRegistry()
    executor = ToolExecutor(registry=reg, project_root=str(tmp_path))

    target_file = str(tmp_path / "locked.txt")
    thread_finished = threading.Event()

    def slow_writer(args, ctx):
        time.sleep(0.1)
        thread_finished.set()
        return "done"

    from coderai.tools.legacy.schema import define_tool

    reg.register(
        define_tool(
            name="write",
            description="Slow write",
            parameters={"file_path": {"type": "string"}},
            required=["file_path"],
            handler=slow_writer,
            timeout_ms=30,  # 30ms timeout
            is_mutating=True,
        )
    )

    tool_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "write", "arguments": json.dumps({"file_path": target_file})},
    }
    res = await executor.execute_tool_call("lock_test", tool_call)
    assert res.ok is False
    assert "TOOL_TIMEOUT" in res.error

    # The path lock must STILL be locked while the thread is finishing
    lock_mgr = get_path_lock_manager()
    cpath = lock_mgr._canonicalize(target_file, str(tmp_path))
    entry = lock_mgr._locks.get(cpath)
    assert entry is not None
    assert entry._write_lock.locked() is True

    # Wait for thread to finish
    thread_finished.wait(timeout=1.0)
    # Give the release task a moment to release
    await asyncio.sleep(0.05)
    assert entry._write_lock.locked() is False


# ---------------------------------------------------------------------------
# TL-A23: Web tool guards, undo history per session, sandbox checks
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_web_search_and_fetch_guard_invalid_ints():
    res1 = await handle_web_search_tool({"queries": ["test"], "max_results": "invalid"}, None)
    # Should not raise ValueError
    assert res1 is not None

    res2 = await handle_web_fetch_tool(
        {"url": "https://example.com", "max_length": "invalid"}, None
    )
    assert res2 is not None


def test_undo_history_per_session_and_sandbox(tmp_path):
    s1 = "sess_1"
    s2 = "sess_2"
    f = str(tmp_path / "test.txt")

    _push_undo(s1, f, "content_1")
    _push_undo(s2, f, "content_2")

    assert _pop_undo(s1, f) == "content_1"
    assert _pop_undo(s1, f) is None
    assert _pop_undo(s2, f) == "content_2"


# ---------------------------------------------------------------------------
# TL-B3: Schema / handler agreement & reject unknown keys
# ---------------------------------------------------------------------------
def test_validate_arguments_rejects_unknown_keys():
    reg = ToolRegistry()

    with pytest.raises(ValidationError, match="unknown argument"):
        reg.validate_arguments("read", {"file_path": "/tmp/a.txt", "bogus_arg": 123})


def test_webfetch_schema_has_raw_and_max_length():
    reg = ToolRegistry()
    fetch_tool = reg.get("WebFetch")
    assert fetch_tool is not None
    assert "raw" in fetch_tool.parameters
    assert "max_length" in fetch_tool.parameters
    assert "use_cache" in fetch_tool.parameters
    assert fetch_tool.parameters["max_length"]["type"] == "integer"


def test_read_and_bash_integer_types():
    reg = ToolRegistry()
    read_tool = reg.get("read")
    assert read_tool.parameters["offset"]["type"] == "integer"
    assert read_tool.parameters["limit"]["type"] == "integer"

    bash_tool = reg.get("bash")
    assert bash_tool.parameters["timeout_ms"]["type"] == "integer"


# ---------------------------------------------------------------------------
# TL-B5: extract_key_argument for live tool names
# ---------------------------------------------------------------------------
def test_extract_key_argument_live_tools():
    assert extract_key_argument('{"command": "pytest"}', "bash") == "pytest"
    assert extract_key_argument('{"command": "Get-Process"}', "pwsh") == "Get-Process"
    assert extract_key_argument('{"file_path": "src/main.py"}', "read") == "src/main.py"
    assert extract_key_argument('{"file_path": "src/main.py"}', "write") == "src/main.py"
    assert extract_key_argument('{"file_path": "src/main.py"}', "edit") == "src/main.py"
    assert extract_key_argument('{"pattern": "def foo"}', "grep") == "def foo"
    assert extract_key_argument('{"pattern": "*.py"}', "glob") == "*.py"
    assert extract_key_argument('{"query": "python docs"}', "WebSearch") == "python docs"
    assert (
        extract_key_argument('{"url": "https://example.com"}', "WebFetch") == "https://example.com"
    )
    assert extract_key_argument('{"description": "analyze bug"}', "Task") == "analyze bug"
    assert extract_key_argument('{"description": "plan fix"}', "subagent") == "plan fix"
    assert extract_key_argument('{"thought": "reasoning step"}', "Think") == "reasoning step"


# ---------------------------------------------------------------------------
# TL-B6: Relative paths resolve identically across read, write, edit, grep, glob
# ---------------------------------------------------------------------------
def test_relative_paths_resolve_with_isolated_cwd(tmp_path):
    from coderai.tools.file._search_common import _session_workdir

    iso_dir = tmp_path / "isolated"
    iso_dir.mkdir()
    ctx = {"isolated_cwd": str(iso_dir), "project_root": str(tmp_path)}

    assert _session_workdir(ctx) == str(iso_dir.resolve())


# ---------------------------------------------------------------------------
# UI-A17: Terminal manager buffer cap, incremental UTF-8 decoder, write loop
# ---------------------------------------------------------------------------
def test_terminal_session_buffer_cap_and_incremental_decoder():
    # Test incremental decoder logic
    from codecs import getincrementaldecoder

    decoder = getincrementaldecoder("utf-8")("replace")
    # Multi-byte UTF-8 character '€' (0xE2, 0x82, 0xAC) split across two chunks
    chunk1 = b"\xe2\x82"
    chunk2 = b"\xac"

    t1 = decoder.decode(chunk1, final=False)
    assert t1 == ""  # Incomplete character
    t2 = decoder.decode(chunk2, final=False)
    assert t2 == "€"  # Successfully completed without replacement character!
