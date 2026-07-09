"""Coverage for coderAI/tui/slash.py command routing."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from coderAI.tui import slash


class FakeController:
    def __init__(self):
        self.commands = []

    def enqueue_command(self, name, **kwargs):
        self.commands.append((name, kwargs))

    def last(self):
        return self.commands[-1] if self.commands else None

    def names(self):
        return [n for n, _ in self.commands]


class FakeReducer:
    def __init__(self):
        self.timeline = []
        self.session = SimpleNamespace(agents={})
        self.pushed = []
        self._id = 0

    def next_id(self):
        self._id += 1
        return self._id

    def _push(self, item):
        self.pushed.append(item)

    def _bump_refresh(self, mode):
        pass

    def _notify(self):
        pass

    def toast(self, level, message):
        self._push({"kind": "toast", "id": self.next_id(), "level": level, "message": message})


def _dispatch(raw, controller, reducer, **overrides):
    cb = {
        "show_palette": MagicMock(),
        "show_search": MagicMock(),
        "show_context": MagicMock(),
        "clear_context": MagicMock(),
        "toggle_verbose": MagicMock(),
        "reveal_reasoning": MagicMock(),
        "confirm_exit": MagicMock(return_value=True),
        "set_search_filter": MagicMock(),
        "retry_agent": MagicMock(),
        "rewind_timeline": MagicMock(),
        "resume_session": MagicMock(),
    }
    cb.update(overrides)
    handled = slash.handle_slash_command(raw, controller, reducer, **cb)
    return handled, cb


@pytest.fixture
def ctrl():
    return FakeController()


@pytest.fixture
def red():
    return FakeReducer()


def _toast_levels(reducer):
    return [p.get("level") for p in reducer.pushed if p.get("kind") == "toast"]


def test_help_intercepted_before_registry(ctrl, red):
    handled, cb = _dispatch("/help", ctrl, red)
    assert handled
    cb["show_palette"].assert_called_once_with(None)


def test_clear_and_compact(ctrl, red):
    _dispatch("/clear", ctrl, red)
    handled, cb = _dispatch("/clear", ctrl, red)
    cb["clear_context"].assert_called()

    _dispatch("/compact", ctrl, red)
    assert "compact_context" in ctrl.names()


def test_model_variants(ctrl, red):
    _dispatch("/model", ctrl, red)
    assert "list_models" in ctrl.names()

    _dispatch("/model gpt-4", ctrl, red)
    assert ("set_model", {"model": "gpt-4"}) == ctrl.last()

    _dispatch("/model default sonnet", ctrl, red)
    assert ("set_default_model", {"model": "sonnet"}) == ctrl.last()

    # "default" with no name -> usage warning, no command.
    before = len(ctrl.commands)
    _dispatch("/model default", ctrl, red)
    assert len(ctrl.commands) == before
    assert "warning" in _toast_levels(red)


def test_reasoning_variants(ctrl, red):
    _, cb = _dispatch("/reasoning", ctrl, red)
    cb["show_palette"].assert_called_once_with("reasoning")

    _dispatch("/reasoning high", ctrl, red)
    assert ("set_reasoning", {"effort": "high"}) == ctrl.last()

    before = len(ctrl.commands)
    _dispatch("/reasoning bogus", ctrl, red)
    assert len(ctrl.commands) == before  # invalid -> no command


def test_yolo_and_tool_allowlist(ctrl, red):
    _dispatch("/yolo", ctrl, red)
    assert "toggle_auto_approve" in ctrl.names()

    _dispatch("/allow-tool run_command", ctrl, red)
    assert ("allow_tool", {"tool": "run_command"}) == ctrl.last()
    _dispatch("/disallow-tool run_command", ctrl, red)
    assert ("disallow_tool", {"tool": "run_command"}) == ctrl.last()
    _dispatch("/allowed-tools", ctrl, red)
    assert "list_allowed_tools" in ctrl.names()

    # Missing-arg usage paths.
    before = len(ctrl.commands)
    _dispatch("/allow-tool", ctrl, red)
    _dispatch("/disallow-tool", ctrl, red)
    assert len(ctrl.commands) == before


def test_persona_and_skills(ctrl, red):
    _, cb = _dispatch("/persona", ctrl, red)
    assert "list_personas" in ctrl.names()
    cb["show_palette"].assert_called_once_with("personas")
    _dispatch("/persona planner", ctrl, red)
    assert ("set_persona", {"persona": "planner"}) == ctrl.last()

    _, cb = _dispatch("/skills", ctrl, red)
    assert "list_skills" in ctrl.names()
    cb["show_palette"].assert_called_once_with("skills")
    _dispatch("/skills deploy", ctrl, red)
    assert ("send_message", {"text": "/skills deploy"}) == ctrl.last()


def test_mcp_lists_and_toggles(ctrl, red):
    # No arg → fetch the list and open the mcp picker section.
    _, cb = _dispatch("/mcp", ctrl, red)
    assert "list_mcp_servers" in ctrl.names()
    cb["show_palette"].assert_called_once_with("mcp")

    # `/mcp list` behaves the same as no arg.
    _, cb = _dispatch("/mcp list", ctrl, red)
    cb["show_palette"].assert_called_once_with("mcp")

    # A name toggles that server (connect/disconnect handled server-side).
    _dispatch("/mcp fetch", ctrl, red)
    assert ("toggle_mcp_server", {"server": "fetch"}) == ctrl.last()


def test_verbose_think_status(ctrl, red):
    _, cb = _dispatch("/verbose", ctrl, red)
    cb["toggle_verbose"].assert_called_once()
    _, cb = _dispatch("/think", ctrl, red)
    cb["reveal_reasoning"].assert_called_once()


def test_tokens_renders_usage_summary(ctrl, red):
    # /tokens (and its /status alias) refresh the panels AND surface a
    # token/cost summary toast — distinct from /context's pinned-file list.
    red.session = SimpleNamespace(
        agents={},
        model="claude-opus-4-8",
        ctx_used=12000,
        ctx_limit=200000,
        prompt_tokens=8000,
        completion_tokens=4000,
        cost_usd=0.1234,
        budget_usd=0.0,
        context_files=[{"path": "a.py", "size": 10}],
    )
    _, cb = _dispatch("/tokens", ctrl, red)
    assert "get_state" in ctrl.names()
    cb["show_context"].assert_not_called()  # not the pinned-files view
    toast = [p for p in red.pushed if p.get("kind") == "toast"][-1]
    assert toast["level"] == "info"
    assert "Session usage" in toast["message"]
    assert "8,000" in toast["message"]  # prompt tokens
    assert "12,000 / 200,000" in toast["message"]  # context
    # /status is an alias of the same handler.
    _, cb = _dispatch("/status", ctrl, red)
    cb["show_context"].assert_not_called()


def test_context_lists_pinned_files(ctrl, red):
    _, cb = _dispatch("/context", ctrl, red)
    assert "get_state" in ctrl.names()
    cb["show_context"].assert_called_once()


def test_code_search(ctrl, red):
    before = len(ctrl.commands)
    _dispatch("/code-search", ctrl, red)
    assert len(ctrl.commands) == before  # no arg -> warning only
    _dispatch("/code-search foo bar", ctrl, red)
    assert ("search_codebase", {"query": "foo bar"}) == ctrl.last()


def test_agents_tasks_plan(ctrl, red):
    _dispatch("/agents", ctrl, red)
    assert "get_state" in ctrl.names()
    _dispatch("/tasks", ctrl, red)
    assert "get_tasks" in ctrl.names()
    _dispatch("/plan", ctrl, red)
    assert "get_tasks" in ctrl.names()


def test_show_topic_routing(ctrl, red):
    _dispatch("/show plan", ctrl, red)
    assert "get_tasks" in ctrl.names()
    # /show tasks routes to the server's reference handler (text listing); the
    # dedicated /tasks command is what refreshes the panel (get_tasks).
    _dispatch("/show tasks", ctrl, red)
    assert ("reference", {"topic": "tasks"}) == ctrl.last()
    _dispatch("/show config", ctrl, red)
    assert ("reference", {"topic": "config"}) == ctrl.last()
    # Alias form: /version -> reference topic=version.
    _dispatch("/version", ctrl, red)
    assert ("reference", {"topic": "version"}) == ctrl.last()
    # providers normalises to models.
    _dispatch("/providers", ctrl, red)
    assert ("reference", {"topic": "models"}) == ctrl.last()


def test_exit_confirmation(ctrl, red):
    _, cb = _dispatch("/exit", ctrl, red)
    cb["confirm_exit"].assert_called_once()
    # When confirm returns False, a warning toast is pushed.
    _dispatch("/exit", ctrl, red, confirm_exit=MagicMock(return_value=False))
    assert "warning" in _toast_levels(red)


def test_export_writes_file(ctrl, red, tmp_path):
    red.timeline = [{"kind": "user", "text": "hi"}, {"kind": "assistant", "content": "yo"}]
    target = tmp_path / "out.md"
    _dispatch(f"/export {target}", ctrl, red)
    assert target.read_text(encoding="utf-8").startswith("# CoderAI Session")
    assert "success" in _toast_levels(red)


def test_export_failure_toasts_warning(ctrl, red, tmp_path):
    # Point at a path whose parent cannot be created (a file used as a dir).
    blocker = tmp_path / "file"
    blocker.write_text("x")
    target = blocker / "nested" / "out.md"
    _dispatch(f"/export {target}", ctrl, red)
    assert "warning" in _toast_levels(red)


def test_search_variants(ctrl, red):
    _, cb = _dispatch("/search", ctrl, red)
    cb["show_search"].assert_called()
    _, cb = _dispatch("/search needle", ctrl, red)
    cb["set_search_filter"].assert_called_once_with("needle")


def test_pin_unpin(ctrl, red):
    _dispatch("/pin src/app.py", ctrl, red)
    assert ("manage_context", {"action": "add", "path": "src/app.py"}) == ctrl.last()
    _dispatch("/unpin src/app.py", ctrl, red)
    assert ("manage_context", {"action": "remove", "path": "src/app.py"}) == ctrl.last()
    before = len(ctrl.commands)
    _dispatch("/pin", ctrl, red)
    _dispatch("/unpin", ctrl, red)
    assert len(ctrl.commands) == before  # missing-arg warnings only


def test_copy_with_and_without_assistant(ctrl, red, capsys):
    # No assistant response yet -> warning.
    _dispatch("/copy", ctrl, red)
    assert "warning" in _toast_levels(red)

    # With an assistant message, OSC-52 + fallback file run (covers clipboard).
    red.timeline = [{"kind": "assistant", "content": "the answer"}]
    _dispatch("/copy", ctrl, red)
    assert "\033]52;c;" in capsys.readouterr().out


def test_retry(ctrl, red):
    _, cb = _dispatch("/retry", ctrl, red)
    cb["retry_agent"].assert_called_once()


def test_resume_without_arg_opens_picker(ctrl, red):
    _, cb = _dispatch("/resume", ctrl, red)
    cb["resume_session"].assert_called_once_with(None)


def test_resume_with_id_resumes_directly(ctrl, red):
    _, cb = _dispatch("/resume session_123_abc", ctrl, red)
    cb["resume_session"].assert_called_once_with("session_123_abc")


def test_resume_alias_sessions(ctrl, red):
    _, cb = _dispatch("/sessions", ctrl, red)
    cb["resume_session"].assert_called_once_with(None)


def test_kill_resolves_name_to_id(ctrl, red):
    red.session.agents = {"agent-7": SimpleNamespace(name="builder")}
    _dispatch("/kill builder", ctrl, red)
    assert ("cancel_agent", {"agentId": "agent-7"}) == ctrl.last()
    # Unknown name falls through to the raw arg as the id.
    _dispatch("/kill ghost", ctrl, red)
    assert ("cancel_agent", {"agentId": "ghost"}) == ctrl.last()
    # Missing arg -> warning.
    before = len(ctrl.commands)
    _dispatch("/kill", ctrl, red)
    assert len(ctrl.commands) == before


def test_init(ctrl, red):
    _dispatch("/init", ctrl, red)
    assert "init_project" in ctrl.names()


def test_undo(ctrl, red):
    _dispatch("/undo", ctrl, red)
    assert ("send_message", {"text": "/undo"}) == ctrl.last()


def test_unknown_command_warns(ctrl, red):
    handled, _ = _dispatch("/nonsense", ctrl, red)
    assert handled
    assert "warning" in _toast_levels(red)


def test_rewind_no_turns_warns(ctrl, red):
    handled, _ = _dispatch("/rewind", ctrl, red)
    assert handled
    assert "warning" in _toast_levels(red)
    assert ctrl.commands == []


def test_rewind_lists_turns_without_arg(ctrl, red):
    red.timeline = [
        {"kind": "user", "text": "first task"},
        {"kind": "assistant", "content": "ok"},
        {"kind": "user", "text": "second task"},
    ]
    handled, _ = _dispatch("/rewind", ctrl, red)
    assert handled
    assert "info" in _toast_levels(red)
    assert ctrl.commands == []  # listing only, nothing enqueued


def test_rewind_valid_turn_truncates_and_enqueues(ctrl, red):
    red.timeline = [
        {"kind": "user", "text": "first"},
        {"kind": "user", "text": "second"},
    ]
    handled, cb = _dispatch("/rewind 1", ctrl, red)
    assert handled
    cb["rewind_timeline"].assert_called_once_with(1)
    assert ("rewind", {"turn": 1, "files": False}) == ctrl.last()


def test_rewind_files_flag_sets_files_true(ctrl, red):
    red.timeline = [{"kind": "user", "text": "first"}]
    _dispatch("/rewind 1 --files", ctrl, red)
    assert ("rewind", {"turn": 1, "files": True}) == ctrl.last()


def test_rewind_invalid_turn_warns_and_does_nothing(ctrl, red):
    red.timeline = [{"kind": "user", "text": "first"}]
    handled, cb = _dispatch("/rewind 9", ctrl, red)
    assert handled
    assert "warning" in _toast_levels(red)
    cb["rewind_timeline"].assert_not_called()
    assert ctrl.commands == []


def test_find_last_assistant_helper():
    timeline = [
        {"kind": "user", "text": "q"},
        {"kind": "assistant", "content": "first"},
        {"kind": "tool", "name": "t"},
        {"kind": "assistant", "content": "latest"},
    ]
    assert slash._find_last_assistant(timeline) == "latest"
    assert slash._find_last_assistant([{"kind": "user", "text": "q"}]) is None


# ── help menu / registry sync ──────────────────────────────────────────


def test_every_command_spec_appears_in_help_menu():
    from coderAI.tui.help_menu import HELP_MENU_ENTRIES

    help_commands = {cmd for cmd, _ in HELP_MENU_ENTRIES}
    for spec in slash.COMMAND_SPECS:
        assert "/" + spec.names[0] in help_commands


def test_every_help_entry_resolves_in_registry():
    from coderAI.tui.help_menu import HELP_MENU_ENTRIES

    for cmd, desc in HELP_MENU_ENTRIES:
        name = cmd.lstrip("/")
        spec = slash._SLASH_REGISTRY.get(name)
        assert spec is not None, f"{cmd} listed in help but not registered"
        assert spec.desc == desc


def test_aliases_share_spec_with_primary():
    for spec in slash.COMMAND_SPECS:
        for name in spec.names:
            assert slash._SLASH_REGISTRY[name] is spec
