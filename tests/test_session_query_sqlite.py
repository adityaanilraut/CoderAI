"""Tests for Session Query SQLite Index, full-text search, and tool execution."""

from __future__ import annotations

import pytest
from coderai.core.session_query.sqlite_index import SessionSqliteIndex
from coderai.core.session_query.engine import SessionQueryEngine
from coderai.core.tools.session_query import handle_session_query_tool


def test_sqlite_indexing_and_search(tmp_path):
    db_path = tmp_path / "index.db"
    idx = SessionSqliteIndex(db_path)

    # 1. Index session
    idx.index_session_header("sess_abc", title="Refactor Auth Module")

    # 2. Index events
    idx.index_event(
        "sess_abc",
        seq=1,
        event_type="user_message",
        role="user",
        content="Please refactor the JWT authentication token verification.",
    )
    idx.index_event(
        "sess_abc",
        seq=2,
        event_type="assistant_message",
        role="assistant",
        content="I have updated auth/jwt.py with RSA256 signature verification.",
    )

    # 3. Search events with FTS5
    hits = idx.search_events("JWT authentication")
    assert len(hits) > 0
    assert hits[0].session_id == "sess_abc"
    assert "JWT" in hits[0].snippet or "authentication" in hits[0].snippet

    # 4. Search by role
    hits_asst = idx.search_events("verification", role="assistant")
    assert len(hits_asst) > 0
    assert hits_asst[0].role == "assistant"

    # 5. List sessions
    sessions = idx.list_sessions()
    assert len(sessions) == 1
    assert sessions[0].title == "Refactor Auth Module"


@pytest.mark.asyncio
async def test_session_query_tool(tmp_path, monkeypatch):
    db_path = tmp_path / "index.db"
    idx = SessionSqliteIndex(db_path)
    idx.index_session_header("sess_100", title="Fix Database Connection Pool")
    idx.index_event(
        "sess_100",
        seq=1,
        event_type="tool_execution",
        role="tool",
        tool_name="bash",
        content="psql -h localhost -U postgres error: connection refused",
    )

    engine = SessionQueryEngine(project_root=str(tmp_path))
    engine.sqlite_index = idx

    monkeypatch.setattr(
        "coderai.core.tools.session_query.SessionQueryEngine",
        lambda project_root: engine,
    )

    ctx = type("Ctx", (), {"project_root": str(tmp_path), "session_id": "current"})()

    # Search action
    res_search = await handle_session_query_tool(
        {"action": "search", "query": "connection refused"}, ctx
    )
    assert res_search.ok is True
    assert "connection refused" in res_search.output
    assert "sess_100" in res_search.output

    # List action
    res_list = await handle_session_query_tool({"action": "list"}, ctx)
    assert res_list.ok is True
    assert "Fix Database Connection Pool" in res_list.output
