"""Tests for Third-Pass Parity, Hardening & Codebase Cleanup.

Validates:
1. Spill cleanup functions (session spill purging and process-wide cleanup).
2. JobStore kill_all lifecycle method and session-scoped job cancellation.
3. Binary file detection in file_utils and read tool.
4. Line ending normalization and invariant typing.
5. Session deletion and dispose resource teardown.
6. Tool executor argument parsing resilience (dicts, malformed JSON, repaired JSON).
7. Terminal manager persistent session cleanup.
"""

from __future__ import annotations

import pathlib
import tempfile

from coderai.core.common.file_utils import is_binary_buffer, normalize_line_endings
from coderai.core.common.invariants import (
    verify_paired_tool_calls,
)
from coderai.core.jobs import JobStore
from coderai.core.spill import (
    save_text,
    cleanup_spill_session,
    cleanup_all_spills,
    private_root,
)
from coderai.core.terminal.manager import TerminalManager
from coderai.core.tools.executor import ToolExecutor
from coderai.core.tools.read import handle_read_tool
from coderai.core.tools.types import ToolExecutionContext


# --- 1. Spill Store Lifecycle Tests ---

def test_spill_session_cleanup():
    """Verify that cleanup_spill_session purges spilled files for target session."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = pathlib.Path(tmpdir)
        ref1 = save_text(
            session_id="session_alpha",
            suggested_name="output1.txt",
            content="Alpha content",
            root=root,
        )
        ref2 = save_text(
            session_id="session_beta",
            suggested_name="output2.txt",
            content="Beta content",
            root=root,
        )

        assert pathlib.Path(ref1.locator).exists()
        assert pathlib.Path(ref2.locator).exists()

        # Clean up only session_alpha
        cleanup_spill_session("session_alpha", root=root)

        assert not pathlib.Path(ref1.locator).exists()
        assert pathlib.Path(ref2.locator).exists()

        # Clean up session_beta
        cleanup_spill_session("session_beta", root=root)
        assert not pathlib.Path(ref2.locator).exists()


def test_cleanup_all_spills():
    """Verify cleanup_all_spills removes default root directory."""
    root = private_root()
    assert root.exists()
    cleanup_all_spills()
    assert not root.exists()


# --- 2. JobStore kill_all Tests ---

def test_job_store_kill_all_session_scoped():
    """Verify JobStore.kill_all cancels only specified session jobs or all jobs."""
    store = JobStore()
    j1 = store.start(job_id="job_1", session_id="ses_A", kind="bash", label="Job 1")
    j2 = store.start(job_id="job_2", session_id="ses_A", kind="bash", label="Job 2")
    j3 = store.start(job_id="job_3", session_id="ses_B", kind="bash", label="Job 3")

    assert j1.status == "running"
    assert j2.status == "running"
    assert j3.status == "running"

    killed = store.kill_all(session_id="ses_A", reason="Session closed")
    assert set(killed) == {"job_1", "job_2"}

    assert store.get("job_1", "ses_A").status in ("stopping", "killed")
    assert store.get("job_2", "ses_A").status in ("stopping", "killed")
    assert store.get("job_3", "ses_B").status == "running"

    # Kill remaining
    killed_all = store.kill_all()
    assert killed_all == ["job_3"]
    assert store.get("job_3", "ses_B").status in ("stopping", "killed")


# --- 3. Binary File Detection & Safe Read Tests ---

def test_is_binary_buffer_detection():
    """Verify binary vs text detection on raw byte buffers."""
    # Text buffers
    assert not is_binary_buffer(b"Hello world!\nThis is standard text.")
    assert not is_binary_buffer(b"def foo():\n    return 42\n")
    assert not is_binary_buffer(b"")

    # Binary buffers (null bytes or high non-printable ratio)
    assert is_binary_buffer(b"ELF\x7f\x02\x01\x01\x00\x00\x00\x00\x00")
    assert is_binary_buffer(b"PK\x03\x04\x14\x00\x00\x00\x08\x00")
    assert is_binary_buffer(bytes([0, 1, 2, 3, 4, 5, 6, 7]))


def test_read_tool_handles_binary_files_safely(tmp_path):
    """Verify read tool returns clean warning for generic binary files without corrupting context."""
    bin_file = tmp_path / "sample.bin"
    bin_file.write_bytes(b"\x00\x01\x02\x03\x04\x05\x06\x07\xff\xfe\x00\x00binarydata")

    context = ToolExecutionContext(session_id="test_ses", project_root=str(tmp_path))
    result = handle_read_tool({"file_path": str(bin_file)}, context)

    assert result.ok is True
    assert result.output == "WARNING: File is binary."
    assert result.metadata is not None
    assert result.metadata.get("isBinary") is True
    assert result.metadata.get("bytes") == len(bin_file.read_bytes())


# --- 4. Invariant Verification & Typing Tests ---

def test_invariants_verification_contracts():
    """Verify session invariant checkers report correct diagnostics."""
    # Paired tool calls check
    events = [
        {"role": "user", "type": "turn/start"},
        {"role": "assistant", "tool_calls": [{"id": "call_1", "function": {"name": "read"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "file content"},
        {"role": "user", "type": "turn/start"},
    ]
    violations = verify_paired_tool_calls(events)
    assert isinstance(violations, list)
    assert len(violations) == 0

    # Unpaired tool call check
    bad_events = [
        {"role": "user", "type": "turn/start"},
        {"role": "assistant", "tool_calls": [{"id": "call_orphan", "function": {"name": "bash"}}]},
        {"role": "user", "type": "turn/start"},
    ]
    bad_violations = verify_paired_tool_calls(bad_events)
    assert len(bad_violations) > 0
    assert "call_orphan" in bad_violations[0]


def test_normalize_line_endings():
    """Verify CRLF and CR normalization to LF."""
    assert normalize_line_endings("line1\r\nline2\rline3\n") == "line1\nline2\nline3\n"


# --- 5. Tool Executor Argument Resilience Tests ---

def test_tool_executor_parse_arguments():
    """Verify ToolExecutor._parse_tool_arguments handles dicts, valid JSON, and repaired JSON."""
    executor = ToolExecutor(project_root=".")

    # Direct dict
    res = executor._parse_tool_arguments({"command": "ls -la"})
    assert res["ok"] is True
    assert res["args"] == {"command": "ls -la"}

    # Clean JSON string
    res = executor._parse_tool_arguments('{"command": "echo test", "timeout": 30}')
    assert res["ok"] is True
    assert res["args"]["command"] == "echo test"

    # Markdown-fenced JSON string
    res = executor._parse_tool_arguments('```json\n{"path": "test.txt"}\n```')
    assert res["ok"] is True
    assert res["args"]["path"] == "test.txt"

    # Truncated repairable JSON string
    res = executor._parse_tool_arguments('{"path": "test.txt", "flag": tru')
    assert res["ok"] is True
    assert res["args"]["flag"] is True

    # Empty string
    res = executor._parse_tool_arguments("")
    assert res["ok"] is True
    assert res["args"] == {}

    # Invalid non-object JSON
    res = executor._parse_tool_arguments("12345")
    assert res["ok"] is False
    assert "JSON object" in res["error"]


# --- 6. TerminalManager Lifecycle Tests ---

def test_terminal_manager_close_all():
    """Verify TerminalManager closes all sessions properly."""
    mgr = TerminalManager()
    t1 = mgr.open_session(command=["echo", "hello"])
    assert t1.session_id in mgr._sessions

    mgr.close_all()
    assert len(mgr._sessions) == 0


# --- 7. Session Manager Lifecycle Teardown Tests ---

def test_session_manager_dispose_cleans_resources():
    """Verify SessionManager.dispose() cleans jobs, terminals, and controller events."""
    from coderai.cli.session_factory import build_session_manager

    with tempfile.TemporaryDirectory() as tmpdir:
        sm = build_session_manager(tmpdir)
        sm.job_store.start(job_id="job_live", session_id="ses_1", kind="bash", label="Live Job")
        assert sm.job_store.get("job_live", "ses_1").status == "running"

        sm.dispose()
        assert sm.job_store.get("job_live", "ses_1").status in ("stopping", "killed")


def test_session_delete_cleans_spill_and_jobs():
    """Verify delete_session purges session background jobs and spill files."""
    from coderai.cli.session_factory import build_session_manager

    with tempfile.TemporaryDirectory() as tmpdir:
        sm = build_session_manager(tmpdir)
        # Create an entry
        ses_id = sm._create_empty_session()
        sm.job_store.start(job_id=f"job_{ses_id}", session_id=ses_id, kind="bash", label="Job")

        # Save a spill file
        spill_ref = save_text(session_id=ses_id, suggested_name="test.txt", content="Spilled text")
        assert pathlib.Path(spill_ref.locator).exists()

        # Delete session
        deleted = sm.delete_session(ses_id)
        assert deleted is True
        assert not pathlib.Path(spill_ref.locator).exists()
        assert sm.job_store.get(f"job_{ses_id}", ses_id).status in ("stopping", "killed")

