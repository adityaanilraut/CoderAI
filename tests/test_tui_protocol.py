"""TUI event contract: Python emitters must stay within the declared protocol."""

from __future__ import annotations

import re
from pathlib import Path

from coderAI.tui.protocol import AGENT_EVENT_NAMES

ROOT = Path(__file__).resolve().parents[1]


def _emit_event_names_from_python() -> set[str]:
    names: set[str] = set()
    for rel in (
        "coderAI/ipc/jsonrpc_server.py",
        "coderAI/ipc/streaming.py",
    ):
        text = (ROOT / rel).read_text(encoding="utf-8")
        for m in re.finditer(
            r'(?:self|server)\.emit\(\s*(?:\n\s*)?"([a-zA-Z0-9_]+)"', text, re.MULTILINE
        ):
            names.add(m.group(1))
    return names


def test_python_emits_subset_of_tui_protocol() -> None:
    py = _emit_event_names_from_python()
    declared = set(AGENT_EVENT_NAMES)
    assert py, "expected to find emit() calls in ipc sources"
    assert py.issubset(declared), (
        f"Python IPC emits event names not declared in tui/protocol.py: {sorted(py - declared)}"
    )


def test_protocol_markdown_mentions_bespoke_events() -> None:
    text = (ROOT / "docs/CHAT_EVENTS.md").read_text(encoding="utf-8")
    for ev in ("turn", "tool", "session_patch", "info", "warning", "success"):
        assert f"`{ev}`" in text, f"CHAT_EVENTS.md should document `{ev}`"


def test_protocol_documents_progress_forwarding() -> None:
    docs = (ROOT / "docs/CHAT_EVENTS.md").read_text(encoding="utf-8")
    py = (ROOT / "coderAI/ipc/jsonrpc_server.py").read_text(encoding="utf-8")
    assert "progressKind" in docs
    assert '_bind("tool_progress", self._on_tool_progress)' in py
    assert 'self.emit("progress"' in py
