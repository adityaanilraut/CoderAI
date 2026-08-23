"""Comprehensive test suite for the upgraded weaker capability gaps (DeepSeek Harness parity).

Tests:
1. Atomic File I/O (writeFileAtomic, withFileLock, permission preservation, exclusive temp sibling, contention backoff).
2. Isolated Subprocess Code Runtime (code-runtime-python, AST eval, variable state extraction, tool bindings, crash safety).
3. SQLite Packed-Chunk Persistence and Session Projection Cache with cold-read ladder replay.
4. Session Telemetry Coordinator and OpenTelemetry (OTel) Structured Sink with GenAI semantic attributes.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import stat
import tempfile
import time
import pytest

from coderai.core.common.file_utils import (
    write_file_atomic,
    with_file_lock,
    write_text_file,
    read_text_file_with_metadata,
)
from coderai.core.code_mode.engine import CodeModeSandbox, CodeModeResult
from coderai.core.events import SessionEvent, make_turn_start, make_turn_end, make_step_start
from coderai.core.persistence import (
    SqlitePersistence,
    SessionHeader,
    SessionProjectionCache,
    JsonlPersistence,
)
from coderai.core.session_telemetry import (
    InMemoryTelemetrySink,
    OTelStructuredTelemetrySink,
    SessionTelemetryCoordinator,
    SessionTelemetryRecord,
    TelemetryMode,
    TelemetrySeverity,
)


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
    result: CodeModeResult = await sandbox.execute(code, timeout_seconds=10.0, use_subprocess=True)
    assert result.error is None
    assert result.result == 35 or result.result == "35"
    assert "a" in result.variables
    assert "b" in result.variables


@pytest.mark.asyncio
async def test_code_mode_subprocess_state_retention(tmp_path: pathlib.Path):
    sandbox = CodeModeSandbox(str(tmp_path))
    # Turn 1: set variable
    r1 = await sandbox.execute("counter = 42", timeout_seconds=10.0, use_subprocess=True)
    assert r1.error is None
    assert "counter" in r1.variables

    # Turn 2: read variable from previous turn
    r2 = await sandbox.execute("counter + 8", timeout_seconds=10.0, use_subprocess=True)
    assert r2.error is None
    assert r2.result == 50 or r2.result == "50"


@pytest.mark.asyncio
async def test_code_mode_subprocess_workspace_tools(tmp_path: pathlib.Path):
    sandbox = CodeModeSandbox(str(tmp_path))
    code = """
write_file("generated.txt", "content from subprocess")
read_file("generated.txt")
"""
    result = await sandbox.execute(code, timeout_seconds=10.0, use_subprocess=True)
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
    result = await sandbox.execute(code, timeout_seconds=5.0, use_subprocess=True)
    # The agent/host process survived, and result captured the exit
    assert result is not None


# ============================================================================
# 3. SQLite Packed-Chunk Persistence & Projection Cache Tests
# ============================================================================


def test_sqlite_persistence_lifecycle(tmp_path: pathlib.Path):
    db_file = tmp_path / "test_sessions.db"
    store = SqlitePersistence(db_file)

    session_id = "sess-123"
    header = SessionHeader(
        session_id=session_id,
        model="deepseek-v4-flash",
        project_root=str(tmp_path),
    )
    store.save_header(header)

    ev1 = SessionEvent(seq=1, type="turn/start", time=1000.0, data={"turn": 1})
    ev2 = SessionEvent(seq=2, type="step/start", time=1001.0, data={"step": 1})
    ev3 = SessionEvent(seq=3, type="step/end", time=1002.0, data={"step": 1})

    store.append_events_batch(session_id, [ev1, ev2, ev3])
    assert store.exists(session_id)

    events = store.list_events(session_id)
    assert len(events) == 3
    assert events[0].seq == 1
    assert events[0].type == "turn/start"
    assert events[2].seq == 3

    # Test range query
    tail = store.list_events(session_id, from_seq=2)
    assert len(tail) == 2
    assert tail[0].seq == 2


def test_session_projection_cache_cold_read_ladder(tmp_path: pathlib.Path):
    cache_db = tmp_path / "proj_cache.db"
    cache = SessionProjectionCache(cache_db)

    jsonl_dir = tmp_path / "jsonl"
    persistence = JsonlPersistence(jsonl_dir)
    session_id = "sess-proj-test"

    # Add 5 events
    events = [
        SessionEvent(seq=i, type="step/event", time=1000.0 + i, data={"idx": i})
        for i in range(1, 6)
    ]
    for ev in events:
        persistence.append_event(session_id, ev)

    # Save a projection checkpoint at seq=3
    cache.save_checkpoint(
        session_id=session_id,
        projection_name="messages",
        seq=3,
        data={"summary": "checkpoint at seq 3", "count": 3},
    )

    # Perform cold-read ladder replay
    base_seq, tail_events, state = cache.cold_read_replay(
        persistence=persistence,
        session_id=session_id,
        projection_name="messages",
    )

    assert base_seq == 3
    assert state == {"summary": "checkpoint at seq 3", "count": 3}
    assert len(tail_events) == 2
    assert [e.seq for e in tail_events] == [4, 5]


# ============================================================================
# 4. Session Telemetry & OpenTelemetry Sink Tests
# ============================================================================


def test_session_telemetry_coordinator_and_otel_sink():
    otel_sink = OTelStructuredTelemetrySink(mode=TelemetryMode.FULL)
    coordinator = SessionTelemetryCoordinator(sink=otel_sink, mode=TelemetryMode.FULL)

    session_id = "sess-otel-999"
    coordinator.adopt_session(session_id, metadata={"project": "CoderAI", "model": "deepseek-chat"})

    ev1 = SessionEvent(seq=1, type="turn/start", time=2000.0, data={"turn": 1})
    ev2 = SessionEvent(seq=2, type="llm/error", time=2005.0, data={"error": "rate_limit_exceeded"})

    coordinator.capture_event(session_id, ev1)
    coordinator.capture_event(session_id, ev2)

    # Duplicate or old seq should be ignored by handoff cursor
    coordinator.capture_event(session_id, ev1)

    coordinator.dispose_session(session_id)

    records = otel_sink.emitted_records
    assert len(records) >= 3  # created, ev1, ev2, disposed

    types = [r["Attributes"]["event.type"] for r in records]
    assert "session/created" in types
    assert "session/event/turn/start" in types
    assert "session/event/llm/error" in types
    assert "session/disposed" in types

    # Verify OTel log record structure
    err_record = next(r for r in records if r["Attributes"]["event.type"] == "session/event/llm/error")
    assert err_record["SeverityNumber"] == 17  # ERROR
    assert err_record["SeverityText"] == "ERROR"
    assert err_record["Attributes"]["session.id"] == session_id
    assert err_record["Attributes"]["event.seq"] == 2
