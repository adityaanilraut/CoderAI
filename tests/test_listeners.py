"""Unit tests for the Textual EventReducer."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from coderAI.tui.listeners import EventReducer, STATUS_THROTTLE_S, STREAM_FLUSH_S


def _status_payload(**overrides):
    base = {
        "ctxUsed": 100,
        "ctxLimit": 200000,
        "costUsd": 0.01,
        "budgetUsd": 5.0,
        "promptTokens": 10,
        "completionTokens": 5,
        "totalTokens": 15,
        "iteration": 1,
        "maxIterations": 50,
        "elapsedSeconds": 12.5,
    }
    base.update(overrides)
    return base


def test_turn_start_creates_streaming_assistant() -> None:
    reducer = EventReducer()
    reducer.handle("turn", {"phase": "start"})

    assert len(reducer.timeline) == 1
    item = reducer.timeline[0]
    assert item["kind"] == "assistant"
    assert item["streaming"] is True
    assert reducer.session.thinking is True
    assert reducer.session.streaming is False


def test_turn_text_coalesces_deltas_before_flush() -> None:
    reducer = EventReducer()
    reducer.handle("turn", {"phase": "start"})

    with patch("coderAI.tui.listeners.time.monotonic", return_value=1000.0):
        reducer.handle("turn", {"phase": "text", "delta": "hello "})
        reducer.handle("turn", {"phase": "text", "delta": "world"})

    assert reducer.timeline[0]["content"] == "hello "
    assert reducer._stream_pending_content == "world"
    assert reducer.session.streaming is True


def test_buffered_stream_deltas_do_not_notify_until_flush() -> None:
    reducer = EventReducer()
    notifications: list[None] = []
    reducer.on_change = lambda _mode: notifications.append(None)

    reducer.handle("turn", {"phase": "start"})
    notifications.clear()

    with patch("coderAI.tui.listeners.time.monotonic", return_value=1000.0):
        reducer.handle("turn", {"phase": "text", "delta": "hello "})
        reducer.handle("turn", {"phase": "text", "delta": "world"})

    # First delta flips thinking→streaming; second is buffered only.
    assert len(notifications) == 1


def test_turn_stream_flush_applies_buffered_deltas() -> None:
    reducer = EventReducer()
    reducer.handle("turn", {"phase": "start"})

    with patch("coderAI.tui.listeners.time.monotonic") as mock_mono:
        mock_mono.return_value = 1000.0
        reducer.handle("turn", {"phase": "text", "delta": "hello "})
        reducer.handle("turn", {"phase": "text", "delta": "world"})
        mock_mono.return_value = 1000.0 + STREAM_FLUSH_S + 0.01
        reducer._maybe_flush_stream()

    assert reducer.timeline[0]["content"] == "hello world"
    assert reducer._stream_pending_content == ""


def test_turn_end_merges_pending_and_clears_streaming() -> None:
    reducer = EventReducer()
    reducer.handle("turn", {"phase": "start"})
    reducer.handle("turn", {"phase": "text", "delta": "final answer"})
    reducer.handle("turn", {"phase": "end"})

    assert reducer.timeline[0]["content"] == "final answer"
    assert reducer.timeline[0]["streaming"] is False
    assert reducer.session.streaming is False
    assert reducer.session.thinking is False
    assert reducer._current_assistant_id is None


def test_turn_end_drops_empty_assistant_row() -> None:
    reducer = EventReducer()
    reducer.handle("turn", {"phase": "start"})
    reducer.handle("turn", {"phase": "end"})

    assert reducer.timeline == []


def test_ready_recovers_incomplete_turn() -> None:
    reducer = EventReducer()
    reducer.handle("turn", {"phase": "start"})
    assert reducer.session.thinking is True
    reducer.handle("turn", {"phase": "text", "delta": "partial"})
    assert reducer.timeline[0]["streaming"] is True
    assert reducer.session.streaming is True

    reducer.handle("ready", {})

    assert reducer.timeline[0]["streaming"] is False
    assert reducer.session.thinking is False
    assert reducer.session.streaming is False
    assert reducer._current_assistant_id is None


def test_skill_card_appends_structured_timeline_item() -> None:
    reducer = EventReducer()
    reducer.handle(
        "skill_card",
        {
            "id": "skill_1",
            "name": "security-audit",
            "description": "Run a security pass",
            "steps": [{"index": 1, "label": "Scan dependencies"}],
        },
    )

    item = reducer.timeline[-1]
    assert item["kind"] == "skill_card"
    assert item["id"] == "skill_1"
    assert item["name"] == "security-audit"
    assert item["description"] == "Run a security pass"
    assert item["steps"][0]["label"] == "Scan dependencies"


def test_status_throttling_applies_latest_within_window() -> None:
    reducer = EventReducer()
    t0 = 5000.0

    with patch("coderAI.tui.listeners.time.monotonic") as mock_mono:
        mock_mono.return_value = t0
        reducer.handle("status", _status_payload(ctxUsed=100))
        assert reducer.session.ctx_used == 100

        mock_mono.return_value = t0 + 0.05
        reducer.handle("status", _status_payload(ctxUsed=200, iteration=2))
        assert reducer.session.ctx_used == 100

        mock_mono.return_value = t0 + STATUS_THROTTLE_S + 0.01
        reducer.handle("status", _status_payload(ctxUsed=300, iteration=3))
        assert reducer.session.ctx_used == 300
        assert reducer.session.iteration == 3
        assert reducer.session.max_iterations == 50
        assert reducer.session.elapsed_s == pytest.approx(12.5)


def test_status_fields_map_to_session_state() -> None:
    reducer = EventReducer()
    reducer.handle(
        "status",
        _status_payload(
            ctxUsed=4200,
            iteration=7,
            maxIterations=40,
            elapsedSeconds=99.5,
        ),
    )

    assert reducer.session.ctx_used == 4200
    assert reducer.session.iteration == 7
    assert reducer.session.max_iterations == 40
    assert reducer.session.elapsed_s == pytest.approx(99.5)


def test_awaiting_approval_extended_payload_on_timeline() -> None:
    reducer = EventReducer()
    reducer.handle(
        "tool",
        {
            "id": "tool_42",
            "phase": "awaiting_approval",
            "payload": {
                "name": "write_file",
                "args": {"path": "a.py"},
                "risk": "high",
                "diff": "--- a\n+++ b",
                "requestedBy": "main",
                "parentId": None,
                "iteration": 2,
                "maxIterations": 50,
                "priorApproved": 1,
            },
        },
    )

    item = reducer.pending_approval()
    assert item is not None
    assert item["kind"] == "approval"
    assert item["tool"] == "write_file"
    assert item["diff"] == "--- a\n+++ b"
    assert item["requestedBy"] == "main"
    assert item["iteration"] == 2
    assert item["priorApproved"] == 1


def test_agent_info_from_payload() -> None:
    reducer = EventReducer()
    reducer.handle(
        "agent",
        {
            "phase": "update",
            "info": {
                "id": "agent_sub1",
                "name": "reviewer",
                "status": "thinking",
            },
            "parentId": "agent_main",
        },
    )

    info = reducer.session.agents["agent_sub1"]
    assert info.status == "thinking"
    assert info.name == "reviewer"


def test_tasks_card_updates_session_and_chrome_refresh() -> None:
    reducer = EventReducer()
    modes: list[str] = []
    reducer.on_change = lambda mode: modes.append(mode)

    payload = {
        "summary": "1 in-progress, 1 pending, 0 completed",
        "inProgress": [{"id": 1, "title": "Fix bug", "priority": "high", "status": "in_progress"}],
        "pending": [{"id": 2, "title": "Write tests", "priority": "medium", "status": "pending"}],
        "completed": [],
        "total": 2,
    }
    reducer.handle("tasks_card", {"tasks": payload})

    assert reducer.session.current_tasks == payload
    assert modes == ["chrome"]


# ── context-limit warning toasts ────────────────────────────────────────


def _ctx_toasts(reducer: EventReducer) -> list[str]:
    return [
        str(it.get("message", ""))
        for it in reducer.timeline
        if it.get("kind") == "toast" and "Context" in str(it.get("message", ""))
    ]


def test_hello_seeds_welcome_block_on_empty_timeline() -> None:
    reducer = EventReducer()
    reducer.handle("hello", {"model": "m1", "provider": "P", "cwd": "/proj"})
    welcomes = [it for it in reducer.timeline if it.get("kind") == "welcome"]
    assert len(welcomes) == 1
    assert welcomes[0]["model"] == "m1"
    assert welcomes[0]["provider"] == "P"
    assert welcomes[0]["cwd"] == "/proj"


def test_rehello_on_populated_timeline_skips_welcome() -> None:
    reducer = EventReducer()
    reducer.handle("hello", {"model": "m1"})
    reducer._push({"kind": "user", "id": reducer.next_id(), "text": "hi"})
    # e.g. agent restart after /retry re-emits hello.
    reducer.handle("hello", {"model": "m1"})
    welcomes = [it for it in reducer.timeline if it.get("kind") == "welcome"]
    assert len(welcomes) == 1


def test_ctx_warning_fires_once_at_80_and_90() -> None:
    reducer = EventReducer()
    reducer._apply_status(_status_payload(ctxUsed=160_000, ctxLimit=200_000))  # 80%
    assert len(_ctx_toasts(reducer)) == 1
    assert "80%" in _ctx_toasts(reducer)[0]

    # Staying between thresholds fires nothing more.
    reducer._apply_status(_status_payload(ctxUsed=170_000, ctxLimit=200_000))  # 85%
    assert len(_ctx_toasts(reducer)) == 1

    reducer._apply_status(_status_payload(ctxUsed=185_000, ctxLimit=200_000))  # 92.5%
    toasts = _ctx_toasts(reducer)
    assert len(toasts) == 2
    assert "90%" in toasts[1]

    # Repeated statuses above 90% stay quiet.
    reducer._apply_status(_status_payload(ctxUsed=190_000, ctxLimit=200_000))
    assert len(_ctx_toasts(reducer)) == 2


def test_ctx_warning_rearms_after_dropping_below_75() -> None:
    reducer = EventReducer()
    reducer._apply_status(_status_payload(ctxUsed=185_000, ctxLimit=200_000))  # 92.5%
    assert len(_ctx_toasts(reducer)) == 1

    reducer._apply_status(_status_payload(ctxUsed=100_000, ctxLimit=200_000))  # 50% (compacted)
    reducer._apply_status(_status_payload(ctxUsed=165_000, ctxLimit=200_000))  # 82.5%
    toasts = _ctx_toasts(reducer)
    assert len(toasts) == 2
    assert "80%" in toasts[1]


def test_ctx_warning_silent_without_limit_or_below_threshold() -> None:
    reducer = EventReducer()
    reducer._apply_status(_status_payload(ctxUsed=100, ctxLimit=0))
    reducer._apply_status(_status_payload(ctxUsed=100_000, ctxLimit=200_000))  # 50%
    assert _ctx_toasts(reducer) == []
