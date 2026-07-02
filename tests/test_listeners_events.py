"""Coverage for the remaining EventReducer.handle() branches in tui/listeners.py."""

from coderAI.tui.listeners import EventReducer


def _reducer():
    r = EventReducer()
    r.refreshes = []
    r.on_change = lambda mode: r.refreshes.append(mode)
    return r


def _kinds(r):
    return [it.get("kind") for it in r.timeline]


def test_hello_populates_session():
    r = _reducer()
    r.handle(
        "hello",
        {
            "model": "claude-opus-4-8",
            "provider": "anthropic",
            "cwd": "/tmp",
            "version": "0.3.0",
            "contextLimit": 200000,
            "budgetLimit": 5.0,
            "autoApprove": True,
            "reasoning": "high",
        },
    )
    s = r.session
    assert s.model == "claude-opus-4-8" and s.provider == "anthropic"
    assert s.cwd == "/tmp"
    assert s.ctx_limit == 200000 and s.budget_usd == 5.0
    assert s.auto_approve is True and s.reasoning == "high"
    assert "full" in r.refreshes


def test_turn_reasoning_delta_first_flips_thinking_to_streaming():
    r = _reducer()
    r.handle("turn", {"phase": "start"})
    r.handle("turn", {"phase": "reasoning", "delta": "pondering"})
    assert r.session.thinking is False
    assert r.session.streaming is True
    assert r.timeline[0]["reasoning"] == "pondering"


def test_tool_queued_then_ok_updates_row():
    r = _reducer()
    r.handle("tool", {"id": "t1", "phase": "queued", "payload": {"name": "read_file"}})
    # Duplicate queued/running for the same id does not add a second row.
    r.handle("tool", {"id": "t1", "phase": "running", "payload": {"name": "read_file"}})
    assert _kinds(r).count("tool") == 1

    r.handle(
        "tool",
        {"id": "t1", "phase": "ok", "payload": {"preview": "done", "fullAvailable": True}},
    )
    row = next(it for it in r.timeline if it["kind"] == "tool")
    assert row["ok"] is True
    assert row["preview"] == "done"
    assert row["full_available"] is True


def test_tool_err_marks_failure():
    r = _reducer()
    r.handle("tool", {"id": "t2", "phase": "running", "payload": {"name": "run"}})
    r.handle("tool", {"id": "t2", "phase": "err", "payload": {"error": "boom"}})
    row = next(it for it in r.timeline if it["kind"] == "tool")
    assert row["ok"] is False
    assert row["error"] == "boom"


def test_tool_awaiting_approval_and_pending_helper():
    r = _reducer()
    r.handle(
        "tool",
        {"id": "a1", "phase": "awaiting_approval", "payload": {"name": "delete_file"}},
    )
    pending = r.pending_approval()
    assert pending is not None
    assert pending["tool"] == "delete_file"
    assert pending["decided"] == "pending"


def test_tool_cancelled_with_timeout_marks_denied_and_failed():
    r = _reducer()
    r.handle("tool", {"id": "x", "phase": "awaiting_approval", "payload": {"name": "t"}})
    r.handle("tool", {"id": "x", "phase": "running", "payload": {"name": "t"}})
    r.handle("tool", {"id": "x", "phase": "cancelled", "payload": {"timeoutSeconds": 30}})

    approval = next(it for it in r.timeline if it["kind"] == "approval")
    tool = next(it for it in r.timeline if it["kind"] == "tool")
    assert approval["decided"] == "denied"
    assert tool["ok"] is False
    assert "timed out after 30s" in tool["error"]


def test_agent_event_registers_agent():
    r = _reducer()
    r.handle("agent", {"info": {"id": "ag1", "name": "worker", "status": "thinking"}})
    assert "ag1" in r.session.agents
    assert r.session.agents["ag1"].name == "worker"


def test_available_lists_and_context_state():
    r = _reducer()
    r.handle("available_models", {"models": {"anthropic": ["opus"]}})
    r.handle("available_personas", {"personas": ["planner"]})
    r.handle("available_skills", {"skills": [{"name": "deploy"}]})
    r.handle("context_state", {"files": [{"path": "a.py"}]})
    assert r.session.available_models == {"anthropic": ["opus"]}
    assert r.session.available_personas == ["planner"]
    assert r.session.available_skills == [{"name": "deploy"}]
    assert r.session.context_files == [{"path": "a.py"}]


def test_session_patch_updates_fields():
    r = _reducer()
    r.handle(
        "session_patch",
        {
            "model": "sonnet",
            "provider": "anthropic",
            "autoApprove": True,
            "reasoning": "low",
            "persona": "architect",
        },
    )
    s = r.session
    assert s.model == "sonnet" and s.provider == "anthropic"
    assert s.auto_approve is True and s.reasoning == "low"
    assert s.active_persona == "architect"
    # Empty persona clears it.
    r.handle("session_patch", {"persona": ""})
    assert r.session.active_persona is None


def test_file_diff_appends_diff_item():
    r = _reducer()
    r.handle("file_diff", {"path": "f.py", "diff": "+added"})
    item = r.timeline[-1]
    assert item["kind"] == "diff"
    assert item["path"] == "f.py"
    assert item["diff"] == "+added"


def test_plan_update_sets_plan_and_toasts():
    r = _reducer()
    r.handle(
        "plan_update",
        {"plan": {"title": "Big plan", "completed": 1, "total": 3, "currentIdx": 0}},
    )
    assert r.session.current_plan["title"] == "Big plan"
    toast = next(it for it in r.timeline if it["kind"] == "toast")
    assert "Big plan" in toast["message"]

    # Plan with no title -> generic message.
    r2 = _reducer()
    r2.handle("plan_update", {"plan": {}})
    assert any("Plan updated" in it.get("message", "") for it in r2.timeline)


def test_info_warning_success_toasts():
    r = _reducer()
    for level in ("info", "warning", "success"):
        r.handle(level, {"message": f"{level} msg"})
    toasts = [it for it in r.timeline if it["kind"] == "toast"]
    assert {t["level"] for t in toasts} == {"info", "warning", "success"}


def test_error_event_recovers_and_appends():
    r = _reducer()
    r.handle("turn", {"phase": "start"})
    r.handle("error", {"message": "kaboom", "hint": "retry", "category": "internal"})
    err = next(it for it in r.timeline if it["kind"] == "error")
    assert err["message"] == "kaboom"
    assert err["hint"] == "retry"
    assert r.session.streaming is False


def test_progress_event_sets_session_progress():
    r = _reducer()
    r.handle("progress", {"label": "Indexing", "current": 3, "total": 9, "progressKind": "files"})
    p = r.session.progress
    assert p["label"] == "Indexing" and p["current"] == 3 and p["total"] == 9


def test_goodbye_event():
    r = _reducer()
    r.handle("hello", {"model": "m"})
    r.handle("goodbye", {})
    assert any(it["kind"] == "toast" for it in r.timeline)


def test_unknown_event_is_noop():
    r = _reducer()
    r.handle("not_a_real_event", {})
    assert r.timeline == []
    assert r.refreshes == []  # dirty stayed False, no notify
