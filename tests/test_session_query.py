"""Tests for canonical JSONL session query and tool execution."""

from __future__ import annotations

import pytest

from coderai.core.session_query.engine import SessionQueryEngine
from coderai.core.tools.session_query import handle_session_query_tool


def test_jsonl_search_and_session_listing(tmp_path):
    engine = SessionQueryEngine(str(tmp_path))
    engine.store.replace_rows(
        "sess_abc",
        [
            {
                "seq": 1,
                "role": "user",
                "content": "Please refactor the JWT authentication token verification.",
            },
            {
                "seq": 2,
                "role": "assistant",
                "content": "I updated auth/jwt.py with RSA256 signature verification.",
            },
            {
                "seq": 3,
                "type": "user/message",
                "data": {"content": "Add typed event coverage for refresh tokens."},
            },
        ],
    )
    engine.store.save_index(
        {
            "entries": [
                {
                    "id": "sess_abc",
                    "summary": "Refactor Auth Module",
                    "createTime": "2026-01-01T00:00:00+00:00",
                    "updateTime": "2026-01-01T00:01:00+00:00",
                }
            ]
        }
    )

    hits = engine.search_events("JWT authentication")
    assert len(hits) > 0
    assert hits[0]["sessionId"] == "sess_abc"
    assert "JWT" in hits[0]["snippet"] or "authentication" in hits[0]["snippet"]

    hits_asst = engine.search_events("verification", role="assistant")
    assert len(hits_asst) > 0
    assert hits_asst[0]["role"] == "assistant"

    typed_hits = engine.search_events("refresh tokens", role="user")
    assert typed_hits[0]["role"] == "user"

    sessions = engine.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["title"] == "Refactor Auth Module"
    assert sessions[0]["turnCount"] == 2


@pytest.mark.asyncio
async def test_session_query_tool(tmp_path, monkeypatch):
    engine = SessionQueryEngine(project_root=str(tmp_path))
    engine.store.replace_rows(
        "sess_100",
        [
            {
                "seq": 1,
                "role": "tool",
                "name": "bash",
                "content": "psql -h localhost -U postgres error: connection refused",
            }
        ],
    )
    engine.store.save_index(
        {
            "entries": [
                {
                    "id": "sess_100",
                    "summary": "Fix Database Connection Pool",
                }
            ]
        }
    )

    monkeypatch.setattr(
        "coderai.core.tools.session_query.SessionQueryEngine",
        lambda project_root: engine,
    )

    ctx = type("Ctx", (), {"project_root": str(tmp_path), "session_id": "current"})()

    res_search = await handle_session_query_tool(
        {"action": "search", "query": "connection refused"}, ctx
    )
    assert res_search.ok is True
    assert "connection refused" in res_search.output
    assert "sess_100" in res_search.output

    res_list = await handle_session_query_tool({"action": "list"}, ctx)
    assert res_list.ok is True
    assert "Fix Database Connection Pool" in res_list.output
