"""Event-contract snapshot guard for the UI bridge.

Pins the command names accepted by ``UIBridge`` (``_COMMAND_HANDLERS``) and
the event names it can emit to the TUI. The Phase 2 controller split moves
handlers and serializers into new modules; this test fails loudly if a
command or event name is dropped or renamed in the process.

``docs/CHAT_EVENTS.md`` is the human-readable spec for these names. When a
command or event is intentionally added or removed, update the snapshot
here AND the doc in the same commit.
"""

import re
from pathlib import Path

from coderAI.bridge import controller

BRIDGE_DIR = Path(controller.__file__).parent

EXPECTED_COMMANDS = {
    "send_message",
    "allow_tool",
    "disallow_tool",
    "list_allowed_tools",
    "cancel",
    "cancel_agent",
    "set_model",
    "set_reasoning",
    "set_persona",
    "toggle_auto_approve",
    "tool_approval_resp",
    "clear_context",
    "rewind",
    "compact_context",
    "manage_context",
    "get_state",
    "get_plan",
    "get_tasks",
    "list_models",
    "list_personas",
    "list_skills",
    "search_codebase",
    "reference",
    "set_default_model",
    "set_verbosity",
    "init_project",
    "exit",
}

EXPECTED_EVENTS = {
    "hello",
    "ready",
    "turn",
    "tool",
    "file_diff",
    "status",
    "plan_card",
    "tasks_card",
    "skill_card",
    "agent",
    "session_patch",
    "available_models",
    "available_personas",
    "available_skills",
    "context_state",
    "info",
    "warning",
    "success",
    "error",
    "progress",
    "goodbye",
}

# Matches emit("name", ...) including multi-line calls where the event name
# sits on the following line. Covers self.emit / server.emit / bare emit.
_EMIT_RE = re.compile(r"\bemit\(\s*\n?\s*\"([a-z_]+)\"", re.MULTILINE)


def _emitted_event_names() -> set:
    names = set()
    for py_file in BRIDGE_DIR.glob("**/*.py"):
        names.update(_EMIT_RE.findall(py_file.read_text(encoding="utf-8")))
    # _emit_error wraps emit("error", ...) with a category argument; the
    # regex already catches the inner literal, but assert it explicitly so a
    # refactor of the wrapper can't silently drop the event.
    names.add("error")
    return names


def test_command_names_match_snapshot():
    actual = set(controller._COMMAND_HANDLERS)
    missing = EXPECTED_COMMANDS - actual
    unexpected = actual - EXPECTED_COMMANDS
    assert not missing and not unexpected, (
        f"UI command contract drifted.\nDropped commands: {sorted(missing)}\n"
        f"New/renamed commands (update snapshot + docs/CHAT_EVENTS.md): {sorted(unexpected)}"
    )


def test_emitted_event_names_match_snapshot():
    actual = _emitted_event_names()
    missing = EXPECTED_EVENTS - actual
    unexpected = actual - EXPECTED_EVENTS
    assert not missing and not unexpected, (
        f"UI event contract drifted.\nDropped events: {sorted(missing)}\n"
        f"New/renamed events (update snapshot + docs/CHAT_EVENTS.md): {sorted(unexpected)}"
    )


def test_command_handlers_are_async_callables():
    import inspect

    for name, handler in controller._COMMAND_HANDLERS.items():
        assert inspect.iscoroutinefunction(handler), f"handler for {name!r} must be async"
