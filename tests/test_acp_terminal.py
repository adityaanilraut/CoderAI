"""Tests for the ACP terminal bridge (``coderai/acp/terminal.py`` + ext_method).

All tests use a fake terminal manager: real PTYs are environment-sensitive
(``out of pty devices`` failures are documented in REMAINING_PLAN.md), so
PTY behavior stays covered by the existing terminal tool tests instead.
"""

from __future__ import annotations

import pytest

from coderai.acp.server import ACPServer
from coderai.acp.terminal import TERMINAL_METHODS, TerminalBridge, _clamp_timeout_ms


class _FakeStatus:
    def to_dict(self):
        return {"sessionId": "t", "isAlive": True}


class _FakeTerm:
    def __init__(self, session_id: str, name: str) -> None:
        self.session_id = session_id
        self.name = name
        self.sent: list[tuple[str, bool]] = []
        self.signals: list[str] = []
        self.is_alive = True
        self.exit_code = None

    def send(self, text: str, submit: bool = True) -> None:
        self.sent.append((text, submit))

    def read_available(self, timeout_s: float = 0.1) -> str:
        return f"<out:{self.session_id}>"

    def send_signal(self, sig_name: str) -> None:
        self.signals.append(sig_name)

    def status(self) -> _FakeStatus:
        return _FakeStatus()


class _FakeManager:
    def __init__(self) -> None:
        self.terms: dict[str, _FakeTerm] = {}
        self._next = 1

    def open_session(self, command=None, name=None, cwd=None, env=None) -> _FakeTerm:
        term_id = f"term_{self._next}"
        self._next += 1
        term = _FakeTerm(term_id, name or term_id)
        self.terms[term_id] = term
        return term

    def get_session(self, session_id: str) -> _FakeTerm | None:
        if session_id in self.terms:
            return self.terms[session_id]
        for term in self.terms.values():
            if term.name == session_id:
                return term
        return None

    def close_session(self, session_id: str) -> bool:
        term = self.terms.pop(session_id, None)
        if term is not None:
            term.is_alive = False
            return True
        return False


def _bridge(session_id: str = "acp-1") -> tuple[TerminalBridge, _FakeManager]:
    manager = _FakeManager()
    return TerminalBridge(session_id, work_dir="/tmp", manager=manager), manager


def test_terminal_methods_catalogue():
    assert set(TERMINAL_METHODS) == {"list", "open", "send", "read", "signal", "close"}


def test_bridge_open_list_roundtrip():
    bridge, _ = _bridge()
    assert bridge.list_terminals() == {"terminals": []}
    opened = bridge.open_terminal(command="echo hi", name="work")
    assert "error" not in opened
    listed = bridge.list_terminals()
    assert len(listed["terminals"]) == 1


def test_bridge_send_read_signal_close():
    bridge, _ = _bridge()
    opened = bridge.open_terminal(command="sh")
    # open() returns status dict; recover the owned id via list.
    owned = bridge._owned
    assert len(owned) == 1
    term_id = next(iter(owned))

    sent = bridge.send_terminal(term_id, "echo hi")
    assert sent["output"] == f"<out:{term_id}>"
    assert sent["isAlive"] is True

    read = bridge.read_terminal(term_id)
    assert read["output"] == f"<out:{term_id}>"

    sig = bridge.signal_terminal(term_id, "SIGINT")
    assert sig["signal"] == "SIGINT"

    closed = bridge.close_terminal(term_id)
    assert closed == {"sessionId": term_id, "closed": True}
    assert bridge.list_terminals() == {"terminals": []}
    assert opened["sessionId"] != ""


def test_bridge_isolation_between_acp_sessions():
    bridge_a, _ = _bridge("acp-A")
    bridge_b, manager_b = _bridge("acp-B")
    # Share one underlying manager to prove namespaced ownership.
    bridge_b._manager = bridge_a._manager
    opened = bridge_a.open_terminal(command="sh")
    term_id = next(iter(bridge_a._owned))

    assert bridge_b.list_terminals() == {"terminals": []}
    assert "error" in bridge_b.read_terminal(term_id)
    assert "error" in bridge_b.close_terminal(term_id)
    # Owner still works.
    assert "error" not in bridge_a.read_terminal(term_id)
    assert opened["sessionId"] != ""
    _ = manager_b


def test_bridge_unknown_terminal_is_error_not_raise():
    bridge, _ = _bridge()
    for result in (
        bridge.send_terminal("nope", "hi"),
        bridge.read_terminal("nope"),
        bridge.signal_terminal("nope"),
        bridge.close_terminal("nope"),
    ):
        assert "error" in result


def test_clamp_timeout_defaults_and_caps():
    assert _clamp_timeout_ms(None, 2000) == 2.0
    assert _clamp_timeout_ms("bogus", 2000) == 2.0
    assert _clamp_timeout_ms(-5, 2000) == 2.0
    assert _clamp_timeout_ms(120_000, 2000) == 30.0
    assert _clamp_timeout_ms(500, 2000) == 0.5


@pytest.mark.asyncio
async def test_ext_method_terminal_bridge_catalogue():
    server = ACPServer()
    result = await server.ext_method("terminal/bridge", {})
    assert result == {"operations": [f"terminal/{op}" for op in TERMINAL_METHODS]}


@pytest.mark.asyncio
async def test_ext_method_terminal_list_empty_session():
    server = ACPServer()
    result = await server.ext_method("terminal/list", {"session_id": "acp-new"})
    assert result == {"terminals": []}


@pytest.mark.asyncio
async def test_ext_method_terminal_validation_errors():
    server = ACPServer()
    assert "error" in await server.ext_method("terminal/bogus", {"session_id": "s"})
    assert "error" in await server.ext_method("terminal/list", {})
    assert "error" in await server.ext_method("terminal/read", {"session_id": "s"})
    assert "error" in await server.ext_method(
        "terminal/read", {"session_id": "s", "terminal_id": "missing"}
    )
