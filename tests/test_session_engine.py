"""Consolidated session-engine tests: lifecycle, store, events, compaction, state, retry, jobs, query."""

from __future__ import annotations

import json
import pathlib

import pytest

from coderai.core.common.invariants import verify_session_invariants
from coderai.core.common.llm_retry import (
    classify_llm_failure,
    is_empty_llm_response,
    retry_delay_ms,
)
from coderai.core.compaction import BasicCompaction, ToolResultPruner
from coderai.core.events import (
    SessionEvent,
    derive_messages_from_events,
    make_assistant_event,
    make_compaction_summary,
    make_tool_result_event,
    make_turn_start,
    make_user_event,
)
from coderai.core.jobs import get_job_store, reset_job_store
from coderai.core.session import SessionManager, SessionMessage, get_project_code
from coderai.core.session_query.engine import SessionQueryEngine
from coderai.core.session_state import load_session_state, save_session_state
from coderai.core.session_store import JsonlSessionStore
from coderai.core.tools.jobs import (
    handle_job_kill_tool,
    handle_job_list_tool,
    handle_job_output_tool,
)
from coderai.core.tools.session_query import handle_session_query_tool
from coderai.core.tools.types import TOOL_ABORTED_BEFORE_DISPATCH


def _manager(tmp_path: pathlib.Path, **kwargs) -> SessionManager:
    """Build a SessionManager rooted at tmp_path with a null LLM client."""
    kwargs.setdefault("create_openai_client", lambda: {"client": None})
    kwargs.setdefault("get_resolved_settings", lambda: {})
    return SessionManager(project_root=str(tmp_path), **kwargs)


@pytest.mark.asyncio
async def test_session_lifecycle_create_fork_delete_roundtrip(tmp_path):
    """Create, fork, and delete sessions while preserving forked copies."""
    mgr = _manager(tmp_path)
    sid = await mgr.create_empty_session()
    assert mgr.get_session(sid) is not None
    forked = mgr.fork_session(sid)
    assert forked and forked != sid
    assert mgr.get_session(forked) is not None
    assert mgr.delete_session(sid) is True
    assert mgr.get_session(sid) is None
    assert mgr.get_session(forked) is not None


def test_session_store_layout_uses_project_local_dir(tmp_path):
    """Session storage resolves to the project-local .coderai/sessions dir."""
    proj = tmp_path / "proj"
    proj.mkdir()
    storage = _manager(proj)._storage()
    assert storage["project_dir"] == proj / ".coderai" / "sessions"
    assert storage["project_dir"].exists()


def test_session_store_migration_recovers_global_sessions(tmp_path, monkeypatch):
    """Legacy global sessions migrate into project-local storage on init."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    proj = tmp_path / "proj"
    proj.mkdir()
    code = get_project_code(str(proj))
    gdir = home / ".coderai" / "projects" / code
    (gdir / "images" / "legacy_1").mkdir(parents=True)
    (gdir / "sessions-index.json").write_text(
        json.dumps({"version": 1, "entries": [{"id": "legacy_1", "summary": "old"}]})
    )
    (gdir / "legacy_1.jsonl").write_text(
        json.dumps({"id": "m1", "sessionId": "legacy_1", "role": "user", "content": "hi"}) + "\n"
    )
    (gdir / "images" / "legacy_1" / "a.png").write_bytes(b"\x89PNG")
    mgr = _manager(proj)
    local = proj / ".coderai" / "sessions"
    assert (local / "legacy_1.jsonl").exists()
    assert (local / "images" / "legacy_1" / "a.png").exists()
    assert mgr.get_session("legacy_1").summary == "old"


def test_session_store_replay_recovers_after_corruption(tmp_path):
    """Dangling tool calls are repaired with synthetic aborts and replay cleanly."""
    store = JsonlSessionStore(str(tmp_path))
    sid = "repair_sess"
    store.replace_rows(
        sid,
        [
            {"id": "m1", "session_id": sid, "role": "user", "content": "hello"},
            {
                "id": "m2",
                "session_id": sid,
                "role": "assistant",
                "content": "calling",
                "tool_calls": [
                    {"id": "tc_x", "type": "function", "function": {"name": "f", "arguments": "{}"}}
                ],
            },
            {"id": "m3", "session_id": sid, "role": "user", "content": "next"},
        ],
    )
    assert len(verify_session_invariants(store.read_rows(sid))) > 0
    assert len(store.validate_and_repair_invariants(sid)) > 0
    rows = store.read_rows(sid)
    assert len(verify_session_invariants(rows)) == 0
    abort = next(r for r in rows if r.get("tool_call_id") == "tc_x")
    assert TOOL_ABORTED_BEFORE_DISPATCH in abort.get("content", "")
    assert len(store.replay_events(sid)) == len(rows)


def test_session_store_mixed_rows_read_both_formats(tmp_path):
    """Legacy message rows and typed events coexist in one JSONL log."""
    store = JsonlSessionStore(str(tmp_path))
    sid = "mixed_sess"
    store.replace_rows(
        sid,
        [
            {
                "id": "l1",
                "role": "user",
                "content": "legacy hi",
                "createTime": "2026-01-01T00:00:00+00:00",
            },
            make_assistant_event(seq=1, turn=1, step=1, content="event hi").to_dict(),
        ],
    )
    mgr = _manager(tmp_path)
    msgs = mgr.list_session_messages(sid)
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[0].content == "legacy hi"
    assert msgs[1].content == "event hi"


def test_session_events_taxonomy_serializes_roundtrip():
    """Turn markers round-trip through dict serialization with log-only flags."""
    ev = make_turn_start(seq=0, turn=1)
    assert ev.type == "turn/start" and ev.is_log_only is True and ev.is_surface is False
    back = SessionEvent.from_dict(ev.to_dict())
    assert (back.seq, back.type) == (0, "turn/start")


def test_session_events_derive_messages_skips_log_only():
    """User/assistant/tool events derive while turn/step markers are skipped."""
    evs = [
        make_turn_start(seq=0, turn=1),
        make_user_event(seq=1, content="Hello"),
        make_assistant_event(seq=2, turn=1, step=1, content="Working", tool_calls=[{"id": "c1"}]),
        make_tool_result_event(seq=3, turn=1, step=1, call_id="c1", content="done"),
    ]
    msgs = derive_messages_from_events(evs)
    assert [(m["role"], m["content"]) for m in msgs] == [
        ("user", "Hello"),
        ("assistant", "Working"),
        ("tool", "done"),
    ]


def test_session_events_compaction_shadow_hides_history():
    """Compaction summaries shadow older seqs so only summary plus new turns derive."""
    evs = [
        make_user_event(seq=0, content="old q"),
        make_assistant_event(seq=1, turn=1, step=1, content="old a"),
        make_compaction_summary(
            seq=2, compaction_id="c1", content="Summary here", shadowed_seqs=[0, 1]
        ),
        make_user_event(seq=3, content="new q"),
    ]
    msgs = derive_messages_from_events(evs)
    assert len(msgs) == 2
    assert "Summary here" in msgs[0]["content"]
    assert msgs[1]["content"] == "new q"


def test_session_pruner_truncates_symmetrically():
    """Oversized tool output keeps head and tail with an omission notice."""
    pruner = ToolResultPruner(max_chars=200)
    assert pruner.prune_content("short") == "short"
    long_text = "START_" + "X" * 500 + "_END"
    pruned = pruner.prune_content(long_text)
    assert len(pruned) < len(long_text)
    assert pruned.startswith("START_") and pruned.endswith("_END")
    assert "characters omitted" in pruned


@pytest.mark.asyncio
async def test_session_compaction_preserves_pinned_messages(tmp_path):
    """Pinned messages survive compaction while plain neighbors are shadowed."""
    mgr = _manager(tmp_path, create_openai_client=lambda: {"client": object(), "model": "t"})
    sid = await mgr.create_session("compaction test")
    mgr._update_entry(sid, lambda e: {**e, "status": "idle", "failReason": None})
    msgs = [
        SessionMessage(id="msg_0", session_id=sid, role="system", content="base"),
        SessionMessage(
            id="msg_1",
            session_id=sid,
            role="user",
            content="keep me",
            meta={"pinned": True, "preserve": True},
        ),
        SessionMessage(
            id="msg_2",
            session_id=sid,
            role="assistant",
            content="work",
            tool_calls=[
                {"id": "tc1", "type": "function", "function": {"name": "read", "arguments": "{}"}}
            ],
        ),
        SessionMessage(id="msg_3", session_id=sid, role="tool", content="data", tool_call_id="tc1"),
        SessionMessage(id="msg_4", session_id=sid, role="assistant", content="analysis"),
        SessionMessage(id="msg_5", session_id=sid, role="user", content="next"),
    ]
    mgr.session_store.replace_rows(sid, [m.to_dict() for m in msgs])

    async def _fake_completion(client, request, **kwargs):
        return {
            "choices": [{"message": {"content": "## Primary Request\n- goal"}}],
            "usage": {"total_tokens": 5},
        }

    mgr._create_completion = _fake_completion  # type: ignore[assignment]
    result = await BasicCompaction(manager=mgr).compact_region(sid, start_idx=1, end_idx=5)
    assert result is not None
    assert "msg_1" not in result.shadowed_ids
    assert {"msg_2", "msg_3", "msg_4"} <= set(result.shadowed_ids)


def test_session_state_roundtrip_persists_fields(tmp_path):
    """Custom titles and todos survive a save/load round-trip."""
    d = tmp_path / "ses"
    d.mkdir()
    state = load_session_state(d)
    assert state.version == 1 and state.approval.yolo is False
    state.custom_title = "Hello"
    state.todos.append({"title": "t1", "status": "pending"})  # type: ignore[arg-type]
    save_session_state(state, d)
    reloaded = load_session_state(d)
    assert reloaded.custom_title == "Hello"
    assert reloaded.todos[0].title == "t1"


def test_session_state_corrupt_file_falls_back_default(tmp_path):
    """Corrupt state.json loads defaults instead of raising."""
    d = tmp_path / "ses"
    d.mkdir()
    (d / "state.json").write_text("{broken", encoding="utf-8")
    assert load_session_state(d).custom_title is None


def test_session_retry_classifies_retryable_failures():
    """Rate-limit, server, timeout, and transport errors retry; auth does not."""

    class _E(Exception):
        def __init__(self, code=None, msg=""):
            self.status_code = code
            super().__init__(msg)

    assert classify_llm_failure(_E(429)) == "RATE_LIMIT"
    assert classify_llm_failure(_E(503)) == "SERVER"
    assert classify_llm_failure(_E(msg="Request timed out")) == "TIMEOUT"
    assert classify_llm_failure(_E(msg="Connection error")) == "TRANSPORT"
    assert classify_llm_failure(_E(401)) is None


def test_session_retry_backoff_grows_exponentially_capped():
    """Backoff doubles per attempt and caps at the configured maximum."""
    assert retry_delay_ms(1, random_fn=lambda: 0.5) == 500
    assert retry_delay_ms(2, random_fn=lambda: 0.5) == 1000
    assert retry_delay_ms(20, random_fn=lambda: 0.5) == 10_000


def test_session_retry_empty_response_detects_blank():
    """Empty content without tool calls counts as empty; tool calls do not."""
    assert is_empty_llm_response({"choices": [{"message": {"content": ""}}]}) is True
    assert is_empty_llm_response({"choices": [{"message": {"content": "hi"}}]}) is False
    assert (
        is_empty_llm_response(
            {"choices": [{"message": {"content": "", "tool_calls": [{"id": "1"}]}}]}
        )
        is False
    )


@pytest.mark.asyncio
async def test_session_jobs_lifecycle_lists_outputs_kills(tmp_path):
    """Job list/output/kill tools track deltas and report finished kills."""
    reset_job_store()
    log = tmp_path / "job.log"
    log.write_text("hello\n")
    get_job_store().start(
        job_id="bash-1", session_id="sess", kind="bash", label="echo", output_path=str(log)
    )
    ctx = type("Ctx", (), {"session_id": "sess"})()
    listed = await handle_job_list_tool({}, ctx)
    assert listed.ok and "bash-1" in (listed.output or "")
    assert "hello" in (await handle_job_output_tool({"job_id": "bash-1"}, ctx)).output
    assert "(no new output)" in (await handle_job_output_tool({"job_id": "bash-1"}, ctx)).output
    get_job_store().complete("bash-1", ok=True, exit_code=0)
    killed = await handle_job_kill_tool({"job_id": "bash-1"}, ctx)
    assert killed.ok and "already finished" in (killed.output or "")
    reset_job_store()


def test_session_jobs_kill_all_terminates_running():
    """kill_all transitions every running job in the session to killed."""
    reset_job_store()
    store = get_job_store()
    store.start(job_id="j1", session_id="s1", kind="bash", label="a")
    store.start(job_id="j2", session_id="s1", kind="bash", label="b")
    assert sorted(store.kill_all("s1", reason="test")) == ["j1", "j2"]
    assert all(j.status == "killed" for j in store.list("s1"))
    reset_job_store()


def test_session_query_search_finds_relevant_snippet(tmp_path):
    """Keyword search returns the owning session with a matching snippet."""
    engine = SessionQueryEngine(str(tmp_path))
    engine.store.replace_rows(
        "sess_abc",
        [
            {"seq": 1, "role": "user", "content": "Please refactor the JWT authentication flow."},
            {
                "seq": 2,
                "role": "assistant",
                "content": "Updated auth/jwt.py with RSA verification.",
            },
        ],
    )
    engine.store.save_index({"entries": [{"id": "sess_abc", "summary": "Refactor Auth"}]})
    hits = engine.search_events("JWT authentication")
    assert hits and hits[0]["sessionId"] == "sess_abc"
    assert "JWT" in hits[0]["snippet"] or "authentication" in hits[0]["snippet"]
    assert engine.list_sessions()[0]["title"] == "Refactor Auth"
    trace = engine.get_session_trace("sess_abc")
    assert trace["totalEvents"] == 2
    assert "JWT" in engine.get_event("sess_abc", seq=1)["content"]


@pytest.mark.asyncio
async def test_session_query_tool_handles_search_and_list(tmp_path, monkeypatch):
    """The session-query tool searches by keyword and lists known sessions."""
    engine = SessionQueryEngine(str(tmp_path))
    engine.store.replace_rows(
        "sess_100",
        [{"seq": 1, "role": "tool", "name": "bash", "content": "psql connection refused"}],
    )
    engine.store.save_index({"entries": [{"id": "sess_100", "summary": "Fix Database Pool"}]})
    monkeypatch.setattr(
        "coderai.core.tools.session_query.SessionQueryEngine", lambda project_root: engine
    )
    ctx = type("Ctx", (), {"project_root": str(tmp_path), "session_id": "cur"})()
    found = await handle_session_query_tool(
        {"action": "search", "query": "connection refused"}, ctx
    )
    assert found.ok and "sess_100" in found.output
    listed = await handle_session_query_tool({"action": "list"}, ctx)
    assert listed.ok and "Fix Database Pool" in listed.output


@pytest.mark.asyncio
async def test_session_loop_retry_recovers_after_rate_limit(tmp_path, monkeypatch):
    """Retryable LLM failures retry as new turns without persisting error text."""
    monkeypatch.setattr("coderai.core.session.retry_delay_ms", lambda *_a, **_k: 0)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    calls = {"n": 0}

    class _RateLimit(Exception):
        status_code = 429

        def __str__(self):
            return "Rate limit exceeded"

    def _script(kwargs):
        if any("skillNames" in str(m.get("content", "")) for m in kwargs.get("messages", [])):
            return {
                "choices": [{"message": {"content": '{"skillNames": []}'}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        calls["n"] += 1
        if calls["n"] < 3:
            raise _RateLimit()
        return {
            "choices": [{"message": {"content": "recovered", "tool_calls": None}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    class _Comp:
        def create(self, **kwargs):
            return _script(kwargs)

    class _Chat:
        completions = _Comp()

    class _Client:
        chat = _Chat()

    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {
            "client": _Client(),
            "model": "gpt-4o",
            "thinkingEnabled": False,
        },
        get_resolved_settings=lambda: {
            "model": "gpt-4o",
            "permissions": {"defaultMode": "allowAll"},
        },
    )
    sid = await mgr.create_session("hello", skills=[])
    contents = [m.content for m in mgr.list_session_messages(sid) if m.role == "assistant"]
    assert any("recovered" in (c or "") for c in contents)
    assert not any("Request failed" in (c or "") for c in contents)
    assert calls["n"] == 3
