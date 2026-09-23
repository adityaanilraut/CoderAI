"""Consolidated session-engine tests: lifecycle, store, events, compaction, state, retry, jobs, query."""

from __future__ import annotations

import asyncio
import json
import pathlib
import threading

import pytest

from coderai.utils.common.invariants import verify_session_invariants
from coderai.utils.common.llm_retry import (
    classify_llm_failure,
    is_empty_llm_response,
    retry_delay_ms,
)
from coderai.soul.coderaisoul import AgentLoop
from coderai.soul.compaction import BasicCompaction, ToolResultPruner
from coderai.events import (
    SessionEvent,
    derive_messages_from_events,
    make_assistant_event,
    make_compaction_summary,
    make_tool_result_event,
    make_turn_start,
    make_user_event,
)
from coderai.utils.common.file_history import GitFileHistory
from coderai.background import get_job_store, reset_job_store
from coderai.soul.session.manager import SessionManager, SessionMessage, get_project_code
from coderai.session_state import load_session_state, save_session_state
from coderai.soul.session.store import JsonlSessionStore
from coderai.tools.background import (
    handle_job_kill_tool,
    handle_job_list_tool,
    handle_job_output_tool,
)
from coderai.tools.legacy.types import TOOL_ABORTED_BEFORE_DISPATCH


def _manager(tmp_path: pathlib.Path, **kwargs) -> SessionManager:
    """Build a SessionManager rooted at tmp_path with a null LLM client."""
    kwargs.setdefault("create_openai_client", lambda: {"client": None})
    kwargs.setdefault("get_resolved_settings", lambda: {})
    return SessionManager(project_root=str(tmp_path), **kwargs)


@pytest.mark.asyncio
async def test_create_session_attaches_image_content_params(tmp_path):
    """ACP/CLI image params must land in the user message meta (multimodal path)."""
    mgr = _manager(tmp_path)

    async def _no_activate(session_id, **kwargs):
        return None

    mgr._activate = _no_activate  # type: ignore[method-assign]
    params = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}]
    sid = await mgr.create_session("describe", content_params=params)
    user_msgs = [m for m in mgr.list_session_messages(sid) if m.role == "user"]
    assert user_msgs
    assert (user_msgs[-1].meta or {}).get("contentParams") == params


@pytest.mark.asyncio
async def test_reply_session_appends_image_only_turn(tmp_path):
    """Image-only replies (empty text + params) must still append a message."""
    mgr = _manager(tmp_path)

    async def _no_activate(session_id, **kwargs):
        return None

    mgr._activate = _no_activate  # type: ignore[method-assign]
    sid = await mgr.create_session("first")
    params = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}]
    await mgr.reply_session(sid, "", content_params=params)
    user_msgs = [m for m in mgr.list_session_messages(sid) if m.role == "user"]
    assert len(user_msgs) == 2
    assert (user_msgs[-1].meta or {}).get("contentParams") == params


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


@pytest.mark.asyncio
async def test_session_loop_retry_recovers_after_rate_limit(tmp_path, monkeypatch):
    """Retryable LLM failures retry as new turns without persisting error text."""
    monkeypatch.setattr("coderai.soul.session.manager.retry_delay_ms", lambda *_a, **_k: 0)
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


def test_session_seq_concurrent_mint_unique_and_persistent(tmp_path):
    """Concurrent _next_seq mints are unique; a fresh counter resumes past the log."""
    mgr = _manager(tmp_path)
    sid = "seq_race"
    minted: list[int] = []
    guard = threading.Lock()
    barrier = threading.Barrier(8)

    def _mint() -> None:
        barrier.wait()
        local = [mgr._next_seq(sid) for _ in range(50)]
        with guard:
            minted.extend(local)

    threads = [threading.Thread(target=_mint) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(minted) == list(range(400))
    # Simulate a process restart: drop the in-memory counter and mint again.
    mgr._seq_counters.pop(sid, None)
    for seq in sorted(minted):
        mgr._append_event(sid, make_user_event(seq=seq, content=f"m{seq}"))
    assert mgr._next_seq(sid) == 400


def test_session_agentloop_turn_step_resume_and_atomic(tmp_path):
    """New activations resume turn/step from the log; concurrent claims never duplicate."""
    mgr = _manager(tmp_path)
    sid = "turn_resume"
    first = AgentLoop(mgr, sid)
    assert (first._turn, first._step) == (0, 0)
    first.emit_turn_start()
    first.emit_step_start()
    first.emit_step_start()
    assert (first.turn, first.step) == (1, 2)
    second = AgentLoop(mgr, sid)
    assert (second._turn, second._step) == (1, 2)
    second.emit_turn_start()
    assert second.turn == 2 and second.step == 0
    # Concurrent turn/step claims across activations stay unique (a new
    # turn resets the step counter, so claim the phases separately).
    turns: list[int] = []
    guard = threading.Lock()
    turn_barrier = threading.Barrier(4)

    def _claim_turn() -> None:
        turn_barrier.wait()
        with guard:
            turns.append(mgr.next_turn(sid))

    threads = [threading.Thread(target=_claim_turn) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(turns) == [3, 4, 5, 6]
    steps: list[int] = []
    step_barrier = threading.Barrier(4)

    def _claim_step() -> None:
        step_barrier.wait()
        with guard:
            steps.append(mgr.next_step(sid))

    threads = [threading.Thread(target=_claim_step) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(steps) == [1, 2, 3, 4]


def test_session_controller_claim_release_no_clobber(tmp_path):
    """Activations share (never overwrite) a live controller and release only their own."""
    mgr = _manager(tmp_path)
    sid = "ctrl_race"
    first = AgentLoop(mgr, sid)
    owned = first._claim_controller()
    second = AgentLoop(mgr, sid)
    assert second._claim_controller() is owned
    # Interrupt signals the live controller in place instead of replacing it.
    mgr.interrupt_session(sid)
    assert mgr.session_controllers[sid] is owned
    assert owned.is_set() is True
    assert mgr.is_interrupted(sid) is True
    # A stale activation releasing must not remove a newer controller.
    replacement = asyncio.Event()
    mgr.session_controllers[sid] = replacement
    first._release_controller()
    assert mgr.session_controllers[sid] is replacement
    second._controller = replacement
    second._release_controller()
    assert sid not in mgr.session_controllers


def _append_user(mgr: SessionManager, sid: str, content: str, **meta: str) -> SessionMessage:
    return mgr._build_message(sid, "user", content, meta=dict(meta) if meta else None)


def test_session_undo_retains_target_prompt(tmp_path):
    """Undo reverts to the target checkpoint; the target prompt itself survives."""
    mgr = _manager(tmp_path)
    sid = "undo_keep"
    user1 = _append_user(mgr, sid, "first")
    mgr._append_message(user1)
    mgr._append_message(mgr._build_message(sid, "assistant", "reply one"))
    user2 = _append_user(mgr, sid, "second")
    mgr._append_message(user2)
    mgr._append_message(mgr._build_message(sid, "assistant", "reply two"))
    assert mgr.undo(sid, target_message_id=user1.id, mode="restore_conversation_only") is True
    remaining = mgr.list_session_messages(sid)
    assert [m.id for m in remaining] == [user1.id]


def test_session_undo_partial_code_failure_returns_false(tmp_path):
    """restore_both with an unrestorable checkpoint fails loudly without truncating."""
    mgr = _manager(tmp_path)
    sid = "undo_partial"
    mgr._append_message(_append_user(mgr, sid, "work", checkpointHash="bogus"))
    mgr._append_message(mgr._build_message(sid, "assistant", "did things"))
    before = [m.id for m in mgr.list_session_messages(sid)]
    assert mgr.undo(sid, mode="restore_both") is False
    assert [m.id for m in mgr.list_session_messages(sid)] == before
    with pytest.raises(ValueError):
        mgr.undo(sid, mode="restore_everything")


@pytest.mark.asyncio
async def test_session_compaction_records_shadowed_seqs(tmp_path):
    """Compaction summaries carry the persisted seqs they shadow (derive() hides by seq)."""
    mgr = _manager(tmp_path, create_openai_client=lambda: {"client": object(), "model": "t"})
    sid = "compact_seqs"
    mgr._append_event(sid, make_user_event(seq=0, content="q0", message_id="u0"))
    mgr._append_event(
        sid, make_assistant_event(seq=1, turn=1, step=1, content="a1", message_id="a1")
    )
    mgr._append_event(sid, make_user_event(seq=2, content="q2", message_id="u2"))
    mgr._append_event(
        sid, make_assistant_event(seq=3, turn=1, step=2, content="a3", message_id="a3")
    )

    async def _fake_completion(client, request, **kwargs):
        return {
            "choices": [{"message": {"content": "## Primary Request\n- goal"}}],
            "usage": {"total_tokens": 5},
        }

    mgr._create_completion = _fake_completion  # type: ignore[assignment]
    result = await BasicCompaction(manager=mgr).compact_region(sid, start_idx=1, end_idx=3)
    assert result is not None
    assert result.shadowed_seqs == [1, 2]
    assert set(result.shadowed_ids) == {"a1", "u2"}
    summaries = [e for e in mgr.session_store.list_events(sid) if e.type == "compaction/summary"]
    assert len(summaries) == 1
    assert summaries[0].data["shadowedSeqs"] == [1, 2]
    assert summaries[0].source_event_seqs == [1, 2]


@pytest.mark.asyncio
async def test_session_fork_id_unique_and_seq_seeded(tmp_path):
    """Fork ids are full-entropy/unique and the forked seq counter continues the clone."""
    mgr = _manager(tmp_path)
    sid = await mgr.create_empty_session()
    mgr._append_event(sid, make_turn_start(seq=mgr._next_seq(sid), turn=1))
    mgr._append_event(sid, make_user_event(seq=mgr._next_seq(sid), content="hi"))
    first = mgr.fork_session(sid)
    second = mgr.fork_session(sid)
    assert first and second and first != second
    assert len(first) == 32 and len(second) == 32
    cloned = [r.get("seq") for r in mgr.session_store.read_rows(first)]
    assert mgr._next_seq(first) == max(s for s in cloned if isinstance(s, int)) + 1
    assert mgr.list_session_messages(first)


def test_session_store_orphan_tmp_cleanup(tmp_path):
    """Crash-leftover atomic-write tmps are removed on store init; real logs survive."""
    store = JsonlSessionStore(str(tmp_path))
    store.append_row("keep", {"id": "m1", "role": "user", "content": "hi"})
    orphan = store.project_dir / "keep.jsonl.tmp-deadbeef"
    orphan.write_text("{}\n", encoding="utf-8")
    assert store.cleanup_orphan_tmps() == 1
    assert not orphan.exists()
    assert store.cleanup_orphan_tmps() == 0
    assert store.read_rows("keep") == [{"id": "m1", "role": "user", "content": "hi"}]


def test_session_file_history_fork_errors(tmp_path):
    """Forking file history with bad ids or a missing repo raises instead of no-op."""
    history = GitFileHistory(str(tmp_path), str(tmp_path / "missing" / ".git"))
    with pytest.raises(ValueError):
        history.fork_session("not a valid ref!!!", "target-ok")
    with pytest.raises(ValueError):
        history.fork_session("source-ok", "also bad!!!")
    with pytest.raises(RuntimeError):
        history.fork_session("source-ok", "target-ok")


def test_session_messages_cache_bounded_and_invalidated(tmp_path):
    """The message cache stays bounded and drops entries on writes."""
    mgr = _manager(tmp_path)
    mgr._messages_cache_bound = 2
    sids = []
    for i in range(3):
        sid = f"cache_{i}"
        mgr._append_message(mgr._build_message(sid, "user", f"hello {i}"))
        mgr.list_session_messages(sid)
        sids.append(sid)
    assert len(mgr._messages_cache) <= 2
    mgr._append_message(mgr._build_message(sids[-1], "user", "again"))
    assert sids[-1] not in mgr._messages_cache
    assert mgr.list_session_messages(sids[-1])[-1].content == "again"


def test_classify_api_error_and_retryability() -> None:
    """Ported from the retired KimiSoul suite: telemetry error classification."""
    from kosong.chat_provider import (
        APIConnectionError,
        APIEmptyResponseError,
        APIStatusError,
        APITimeoutError,
    )
    from coderai.soul.coderaisoul import classify_api_error, is_retryable_api_error

    timeout_err = APITimeoutError("Request timed out")
    err_type, code = classify_api_error(timeout_err)
    assert err_type == "timeout"
    assert is_retryable_api_error(timeout_err) is True

    conn_err = APIConnectionError("Connection reset")
    err_type, code = classify_api_error(conn_err)
    assert err_type == "network"
    assert is_retryable_api_error(conn_err) is True

    empty_err = APIEmptyResponseError("Empty body")
    err_type, code = classify_api_error(empty_err)
    assert err_type == "empty_response"
    assert is_retryable_api_error(empty_err) is True

    rate_limit_err = APIStatusError(429, "Rate limited")
    err_type, code = classify_api_error(rate_limit_err)
    assert err_type == "rate_limit"
    assert code == 429
    assert is_retryable_api_error(rate_limit_err) is True

    server_err = APIStatusError(502, "Bad Gateway")
    assert is_retryable_api_error(server_err) is True

    client_err = APIStatusError(404, "Not Found")
    assert is_retryable_api_error(client_err) is False
