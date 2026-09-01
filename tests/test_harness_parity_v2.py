"""Comprehensive tests verifying DeepSeek Harness gold-standard parity in CoderAI."""

import os
import tempfile
import pytest

from coderai.core.common.output_retention import ItemRetainer, TextRetainer
from coderai.core.common.process_tree import (
    scrubbed_parent_env,
)
from coderai.core.session_query.engine import SessionQueryEngine
from coderai.core.session_store import JsonlSessionStore
from coderai.core.subagent_backends.acp import AcpSubagentDriver
from coderai.core.teams.manager import TeamTaskBoard
from coderai.core.tools.observation import FileObservationTracker
from coderai.core.tools.registry import ToolRegistry


def test_output_retention_item_retainer():
    retainer = ItemRetainer(max_items=4)
    items = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    result = retainer.retain(items)
    assert result.total_count == 10
    assert result.retained_count == 4
    assert result.omitted_count == 6
    assert result.items == [1, 2, 9, 10]
    assert "[6 items omitted]" in result.notice


def test_output_retention_text_retainer():
    retainer = TextRetainer(max_chars=100, max_lines=4, line_oriented=True)
    text = "\n".join([f"Line {i}: test content that is long enough" for i in range(20)])
    result = retainer.retain(text)
    assert result.total_lines == 20
    assert result.omitted_lines > 0
    assert "omitted" in result.text


def test_scrubbed_parent_env():
    dirty_env = {
        "PATH": "/usr/bin",
        "OPENAI_API_KEY": "sk-secret-key",
        "AWS_SECRET_ACCESS_KEY": "secret",
        "GITHUB_TOKEN": "ghp_12345",
        "DB_PASSWORD": "pass",
        "USER": "developer",
    }
    cleaned = scrubbed_parent_env(dirty_env)
    assert "PATH" in cleaned
    assert "USER" in cleaned
    assert "OPENAI_API_KEY" not in cleaned
    assert "AWS_SECRET_ACCESS_KEY" not in cleaned
    assert "GITHUB_TOKEN" not in cleaned
    assert "DB_PASSWORD" not in cleaned


def test_team_task_board_cas_locking():
    board = TeamTaskBoard()
    task = board.create_task(title="Build parser", description="Implement AST parser")
    assert task.revision == 1

    # Successful update with correct expected_revision
    updated = board.update_task(
        task_id=task.task_id,
        status="in_progress",
        expected_revision=1,
    )
    assert updated.status == "in_progress"
    assert updated.revision == 2

    # Conflicting update with stale expected_revision must raise ValueError
    with pytest.raises(ValueError) as exc_info:
        board.update_task(
            task_id=task.task_id,
            status="completed",
            expected_revision=1,  # Stale revision!
        )
    assert "ConcurrencyConflictError" in str(exc_info.value)
    assert "revision mismatch" in str(exc_info.value)

    # Valid update with current revision
    updated_again = board.update_task(
        task_id=task.task_id,
        status="completed",
        expected_revision=2,
    )
    assert updated_again.status == "completed"
    assert updated_again.revision == 3


def test_child_tool_scoping_hides_report_from_root():
    registry = ToolRegistry()
    root_tools = registry.to_openai_schemas(options={"childAgent": False})
    root_tool_names = [t["function"]["name"] for t in root_tools]
    assert "report" not in root_tool_names

    child_tools = registry.to_openai_schemas(options={"childAgent": True})
    child_tool_names = [t["function"]["name"] for t in child_tools]
    assert "report" in child_tool_names


def test_granular_session_query_tools_registered():
    registry = ToolRegistry()
    tools = registry.list_tools()
    tool_names = {t.name for t in tools}

    assert "session_query" in tool_names
    assert "session_search" in tool_names
    assert "session_trace" in tool_names
    assert "session_event_search" in tool_names
    assert "session_event_read" in tool_names


def test_session_query_engine_search_and_trace():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = JsonlSessionStore(tmpdir)
        store.append_row(
            "sess-123",
            {
                "type": "message",
                "seq": 1,
                "role": "user",
                "content": "How do I optimize SQL queries?",
            },
        )
        store.append_row(
            "sess-123",
            {
                "type": "message",
                "seq": 2,
                "role": "assistant",
                "content": "You can add indexes to frequently filtered columns.",
            },
        )

        engine = SessionQueryEngine(project_root=tmpdir)
        hits = engine.search_events("optimize SQL")
        assert len(hits) == 1
        assert hits[0]["sessionId"] == "sess-123"

        trace = engine.get_session_trace("sess-123")
        assert trace["totalEvents"] == 2

        event = engine.get_event("sess-123", seq=1)
        assert event is not None
        assert event["role"] == "user"
        assert "optimize SQL" in event["content"]


def test_file_observation_read_before_edit():
    tracker = FileObservationTracker()
    with tempfile.NamedTemporaryFile("w+", delete=False) as f:
        f.write("initial content\n")
        f_path = f.name

    try:
        # Before read -> not allowed
        allowed, err = tracker.check_mutation_allowed("sess-1", f_path, require_observed=True)
        assert not allowed
        assert "FS_NOT_OBSERVED" in (err or "")

        # Record observation
        tracker.record_observation("sess-1", f_path, "initial content\n")
        allowed, err = tracker.check_mutation_allowed("sess-1", f_path, require_observed=True)
        assert allowed

        # External edit modifies file
        with open(f_path, "w") as f2:
            f2.write("external modification\n")

        # Stale check
        allowed, err = tracker.check_mutation_allowed("sess-1", f_path, require_observed=True)
        assert not allowed
        assert "FS_STALE_VERSION" in (err or "")
    finally:
        if os.path.exists(f_path):
            os.unlink(f_path)


def test_acp_subagent_driver_instantiation():
    driver = AcpSubagentDriver(bin_name="acp-agent", timeout_seconds=30.0)
    assert driver.bin_name == "acp-agent"
    assert driver.timeout_seconds == 30.0
