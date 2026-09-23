"""Fixes from the product review: /init, session recall, live /agents, telemetry opt-in."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from coderai.soul.session.models import entry_from_dict
from coderai.soul.session.store import JsonlSessionStore
from coderai.subagents.core import AgentHandle, AgentRegistry
from coderai.telemetry import TransportSink, telemetry_requested
from coderai.tools.legacy.registry import ToolRegistry
from coderai.tools.legacy.types import ToolExecutionContext
from coderai.tools.session.query import (
    handle_session_event_search,
    handle_session_search,
    handle_session_trace,
)
from coderai.ui.shell.dispatch import ShellContext, SlashAction, cmd_agents, cmd_init


def _ctx(mgr: object, session_id: str | None = "s1") -> ShellContext:
    return ShellContext(
        mgr=mgr,
        console=None,
        session_id=session_id,
        yes=False,
        active_plan_mode=False,
    )


def _manager(tmp_path) -> MagicMock:
    store = JsonlSessionStore(str(tmp_path))
    store.save_index(
        {
            "entries": [
                {
                    "id": "abc123",
                    "summary": "fix the parser",
                    "status": "done",
                    "assistantReply": "updated dispatch",
                }
            ]
        }
    )
    store.messages_path("abc123").write_text(
        '{"role":"user","content":"parser bug in dispatch"}\n',
        encoding="utf-8",
    )
    mgr = MagicMock()
    mgr.session_store = store
    mgr.list_sessions.return_value = [
        entry_from_dict(item) for item in store.load_index()["entries"]
    ]
    return mgr


def test_init_queues_agents_prompt() -> None:
    ctx = _ctx(MagicMock())
    assert cmd_init(ctx, "focus on tests") == SlashAction.TURN
    assert ctx.turn_prompt is not None
    assert "AGENTS.md" in ctx.turn_prompt
    assert "focus on tests" in ctx.turn_prompt


def test_session_search_and_trace(tmp_path) -> None:
    mgr = _manager(tmp_path)
    context = ToolExecutionContext(
        session_id="abc123",
        project_root=str(tmp_path),
        session_manager=mgr,
    )
    found = handle_session_search({"query": "parser"}, context)
    assert found.ok
    assert found.output is not None
    assert "abc123" in found.output

    trace = handle_session_trace({"session_id": "abc"}, context)
    assert trace.ok
    assert trace.output is not None
    assert "parser bug" in trace.output

    events = handle_session_event_search({"session_id": "abc123", "query": "dispatch"}, context)
    assert events.ok
    assert events.output is not None
    assert "dispatch" in events.output


def test_session_tools_are_registered() -> None:
    registry = ToolRegistry()
    assert registry.get("session_search") is not None
    assert registry.get("session_query") is not None
    assert registry.get("session_trace") is not None
    assert registry.get("session_event_search") is not None
    assert registry.get("session_event_read") is not None
    assert registry.get("lsp") is None
    assert registry.get("code_mode") is None


def test_agents_send_queues_inbox() -> None:
    registry = AgentRegistry()
    registry.register(
        AgentHandle(
            id="agt_1",
            parent_session_id="s1",
            description="review auth",
            mode="general",
        )
    )
    mgr = MagicMock()
    mgr.agent_registry = registry
    assert cmd_agents(_ctx(mgr, "s1"), "send agt_1 hello there") == SlashAction.HANDLED
    handle = registry.get("agt_1")
    assert handle is not None
    assert handle.inbox == ["hello there"]


def test_telemetry_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODERAI_TELEMETRY", raising=False)
    assert telemetry_requested({}) is False
    assert telemetry_requested({"telemetryEnabled": True}) is True
    monkeypatch.setenv("CODERAI_TELEMETRY", "0")
    assert telemetry_requested({"telemetryEnabled": True}) is False
    monkeypatch.setenv("CODERAI_TELEMETRY", "1")
    assert telemetry_requested({}) is True


def test_transport_sink_flushes() -> None:
    class _Transport:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []

        async def send(self, events: list[dict[str, object]]) -> None:
            self.sent.extend(events)

    transport = _Transport()
    sink = TransportSink(transport)
    sink.accept({"event": "started"})
    sink.flush_sync()
    assert transport.sent == [{"event": "started"}]
    asyncio.run(sink.flush())
