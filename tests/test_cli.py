"""Consolidated CLI tests: parser, slash catalog, renderers, menus, input edges.

Covers (deduped from test_cli_ui / test_cli_extended / test_cli_architecture /
test_cli_review_fixes / model_selection / phase1-3 UI + input suites): parser
purity and flags, slash catalog driving completion, key renderers (diff, plan,
thinking, tool-card, status, file-mention, welcome, exit summary), interactive
menus, and input edge cases. Network/LLM are never touched.
"""

from __future__ import annotations

import pathlib
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Parser purity / flags
# ---------------------------------------------------------------------------


def test_cli_parser_plan_model_prompt_flags_parse() -> None:
    """Parser accepts plan/model/yes/prompt flags without web/server modes."""
    from coderai.ui.shell.app import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["--plan", "--model", "o3-mini", "--yes", "-p", "hello world"])
    assert args.plan is True
    assert args.model == "o3-mini"
    assert args.yes is True
    assert args.prompt_flag == "hello world"
    assert not hasattr(args, "server")


def test_cli_parser_positional_prompt_joins_words() -> None:
    """Positional prompt tokens are collected verbatim in order."""
    from coderai.ui.shell.app import _build_parser

    args = _build_parser().parse_args(["--plan", "--model", "o3-mini", "initial", "prompt"])
    assert args.plan is True
    assert args.model == "o3-mini"
    assert args.prompt == ["initial", "prompt"]


def test_cli_parser_unknown_flag_exits_nonzero() -> None:
    """Removed aliases (e.g. --message) fail parsing instead of running."""
    from coderai.ui.shell.app import _build_parser

    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--message", "hello"])


def test_cli_parser_setup_options_expose_provider_key_model() -> None:
    """Setup flags (--provider/--key/--setup-model/--test/--status) parse."""
    from coderai.ui.shell.app import _build_parser

    parser = _build_parser()
    args = parser.parse_args(
        ["--provider", "deepseek", "--key", "sk-123", "--setup-model", "deepseek-v4-pro"]
    )
    assert args.setup_provider == "deepseek"
    assert args.setup_key == "sk-123"
    assert args.setup_model == "deepseek-v4-pro"
    assert parser.parse_args(["--test"]).setup_test is True
    assert parser.parse_args(["--status"]).setup_status is True


def test_cli_scope_describe_maps_known_scopes() -> None:
    """Scope helpers return human text and risk colors for known scopes."""
    from coderai.ui.shell.app import describe_scope, get_scope_color

    assert describe_scope("read-in-cwd") == "reads inside this workspace"
    assert get_scope_color("read-in-cwd") == "green"
    assert get_scope_color("write-in-cwd") == "yellow"
    assert get_scope_color("write-out-cwd") == "red"


def test_cli_validation_final_message_only_requires_print() -> None:
    """--final-message-only without --print is rejected before any session work."""
    from coderai.ui.shell.app import main

    assert main(["--final-message-only"]) == 1


def test_cli_validation_exec_without_prompt_returns_error() -> None:
    """--exec with no prompt value exits non-zero instead of hanging."""
    from coderai.ui.shell.app import main

    assert main(["--exec"]) == 1


# ---------------------------------------------------------------------------
# Slash catalog -> completion / dispatch
# ---------------------------------------------------------------------------


def test_cli_slash_catalog_covers_completions() -> None:
    """Every catalog command is reachable through the completer list."""
    from coderai.ui.shell.slash import COMMAND_CATALOG
    from coderai.ui.shell.prompt import AVAILABLE_SLASH_COMMANDS

    completed = {name for name, _description in AVAILABLE_SLASH_COMMANDS}
    assert {f"/{name}" for name in COMMAND_CATALOG} <= completed


def test_cli_slash_parse_resolves_alias_and_args() -> None:
    """parse_slash_command resolves aliases and splits trailing args."""
    from coderai.ui.shell.slash import parse_slash_command

    assert parse_slash_command("/settings") == ("/config", "")
    assert parse_slash_command("/job logs 1") == ("/jobs", "logs 1")


def test_cli_completer_suggests_plan_for_prefix(tmp_path: pathlib.Path) -> None:
    """Completing '/pl' suggests '/plan'."""
    from coderai.ui.shell.prompt import CoderAICompleter

    res = CoderAICompleter(str(tmp_path)).complete("/pl", 0)
    assert res is not None
    assert "/plan" in res


def test_cli_completer_suggests_tokens_for_prefix(tmp_path: pathlib.Path) -> None:
    """Completing '/to' suggests '/tokens'."""
    from coderai.ui.shell.prompt import CoderAICompleter

    res = CoderAICompleter(str(tmp_path)).complete("/to", 0)
    assert res is not None
    assert "/tokens" in res


# ---------------------------------------------------------------------------
# Key renderers
# ---------------------------------------------------------------------------


def test_cli_diff_preview_renders_without_crash() -> None:
    """Diff text formats and previews on both real and null consoles."""
    from coderai.utils.rich.diff_render import format_diff_text, render_diff_preview

    sample = "--- a/foo.py\n+++ b/foo.py\n@@ -1,3 +1,3 @@\n-old_line\n+new_line\n context_line\n"
    assert format_diff_text(sample) is not None
    render_diff_preview(None, sample, title="Test Diff")
    mock_console = MagicMock()
    render_diff_preview(mock_console, sample, title="Test Diff")
    assert mock_console.print.called


def test_cli_plan_preview_renders_checklist() -> None:
    """Plan stats count checkboxes and the preview prints to a console."""
    from coderai.ui.shell.visualize._blocks import (
        format_plan_content,
        parse_plan_stats,
        render_plan_preview,
    )

    plan_text = "# Plan\n- [x] Step 1\n- [ ] Step 2\n- [x] Step 3\n"
    total, completed = parse_plan_stats(plan_text)
    assert (total, completed) == (3, 2)
    assert format_plan_content(plan_text) is not None
    mock_console = MagicMock()
    render_plan_preview(mock_console, plan_text, title="Task List")
    assert mock_console.print.called
    render_plan_preview(None, plan_text, title="Task List")


def test_cli_thinking_block_renders_summary() -> None:
    """Long thinking traces are summarized and render collapsed or expanded."""
    from coderai.ui.shell.visualize._blocks import render_thinking_block, summarize_thinking

    trace = "I need to inspect the directory first. Then I will edit app.py to wire the modules."
    summary = summarize_thinking(trace, max_chars=40)
    assert len(summary) <= 40
    assert summary.endswith("...")
    mock_console = MagicMock()
    render_thinking_block(mock_console, trace, elapsed_seconds=1.5, expanded=False)
    assert mock_console.print.called
    render_thinking_block(None, trace, elapsed_seconds=0.8, expanded=True)


def test_cli_tool_card_parses_and_renders() -> None:
    """Tool messages parse to (name, summary, ok, meta) and render as cards."""
    from coderai.ui.shell.visualize._blocks import parse_tool_message, render_tool_card
    from coderai.soul.session.manager import SessionMessage

    msg = SessionMessage(
        id="m1",
        session_id="s1",
        role="tool",
        content='{"name": "edit", "ok": true, "output": "Successfully updated"}',
    )
    name, summary, ok, _meta = parse_tool_message(msg)
    assert name == "edit"
    assert "Successfully updated" in summary
    assert ok is True
    mock_console = MagicMock()
    render_tool_card(mock_console, msg)
    assert mock_console.print.called
    err_msg = SessionMessage(
        id="m2",
        session_id="s1",
        role="tool",
        content='{"name": "bash", "ok": false, "error": "Command not found"}',
    )
    render_tool_card(None, err_msg)


def test_cli_status_bar_formats_model_tokens() -> None:
    """Status bar embeds model, token count, thinking flag, and branch."""
    from coderai.ui.shell.prompt import format_status_bar, render_status_bar

    bar = format_status_bar("gpt-4o", 1250, True, "main")
    text = str(bar)
    assert "gpt-4o" in text and "1,250" in text and "ON" in text and "main" in text
    mock_console = MagicMock()
    render_status_bar(mock_console, "gpt-4o", 1250, False, "/tmp")
    assert mock_console.print.called
    render_status_bar(None, "gpt-4o", 0, True, "/tmp")


def test_cli_file_mention_expands_and_suggests(tmp_path: pathlib.Path) -> None:
    """@file mentions expand to attached context and fuzzy suggestions match."""
    from coderai.ui.shell.prompt import expand_file_mentions, suggest_workspace_files

    (tmp_path / "hello.py").write_text("line1\nline2\nline3\nline4\nline5\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.txt").write_text("nested content\n")
    expanded, attached = expand_file_mentions(
        "Please look at @hello.py:1-3 and explain @nested.txt", str(tmp_path)
    )
    assert len(attached) == 2
    assert "Attached Context: hello.py" in expanded
    suggestions = suggest_workspace_files("nested", str(tmp_path))
    assert any("nested.txt" in s for s in suggestions)


def test_cli_welcome_renders_shortcuts() -> None:
    """Welcome screen renders on null and rich consoles without raising."""
    from coderai.ui.shell import render_welcome_screen

    render_welcome_screen(None, "/tmp", "gpt-4o", plan_mode=True, mcp_servers_count=2)
    mock_console = MagicMock()
    render_welcome_screen(mock_console, "/tmp", "gpt-4o", plan_mode=False, mcp_servers_count=0)
    assert mock_console.print.called


def test_cli_exit_summary_handles_empty_session(tmp_path: pathlib.Path) -> None:
    """Exit summary reports zero turns for a fresh session manager."""
    from coderai.cli.exit_summary import compute_session_stats, render_exit_summary
    from coderai.soul.session.manager import SessionManager

    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": None, "model": "gpt-4o"},
        get_resolved_settings=lambda: {"model": "gpt-4o"},
    )
    assert compute_session_stats(mgr, None)["turns"] == 0
    render_exit_summary(None, mgr, None)
    mock_console = MagicMock()
    render_exit_summary(mock_console, mgr, None)


def test_cli_permissions_auto_approve_allows() -> None:
    """yes=True auto-allows requests for this turn without persisting always-allow."""
    from coderai.ui.shell.app import _prompt_permissions

    requests = [
        {
            "toolCallId": "tc_1",
            "name": "write",
            "command": "write test.py",
            "scopes": ["write-in-cwd"],
            "risk_level": "MODERATE RISK",
            "diff_preview": "--- a/test.py\n+++ b/test.py\n@@ -1 +1 @@\n-old\n+new",
        }
    ]
    replies, always = _prompt_permissions(requests, yes=True)
    assert replies[0]["permission"] == "allow"
    assert always == []


# ---------------------------------------------------------------------------
# Menus
# ---------------------------------------------------------------------------


def test_cli_menu_select_model_by_number(monkeypatch: pytest.MonkeyPatch) -> None:
    """Numeric menu input selects the corresponding curated model."""
    from coderai.ui.shell.session_picker import select_model_interactive

    monkeypatch.setattr("builtins.input", lambda _: "1")
    assert select_model_interactive(None, "gpt-5.6-luna") == "gpt-5.6-sol"
    monkeypatch.setattr("builtins.input", lambda _: "my-custom-model")
    assert select_model_interactive(None, "gpt-4o") == "my-custom-model"


def test_cli_menu_select_arrows_cancel_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling an arrow menu (q) returns None instead of a selection."""
    from coderai.ui.shell.session_picker import select_with_arrows

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "q")
    assert select_with_arrows(None, [("a", "A", "desc A")], allow_cancel=True) is None


def test_cli_menu_select_session_by_number(monkeypatch: pytest.MonkeyPatch) -> None:
    """Numeric session-menu input resumes the chosen session id."""
    from coderai.ui.shell.session_picker import select_session_interactive
    from coderai.soul.session.manager import SessionEntry

    sessions = [
        SessionEntry(id="sess_1234567890", summary="Fix a bug", active_tokens=450),
        SessionEntry(id="sess_abcdefghij", summary="Add feature", active_tokens=890),
    ]
    monkeypatch.setattr("builtins.input", lambda _: "1")
    assert select_session_interactive(None, sessions) == "sess_1234567890"
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert select_session_interactive(None, sessions) is None


# ---------------------------------------------------------------------------
# Input edge cases
# ---------------------------------------------------------------------------


def test_cli_input_normalize_crlf_to_lf() -> None:
    """CRLF line endings are normalized to LF."""
    from coderai.ui.shell.prompt import normalize_multiline_input

    assert normalize_multiline_input("line 1\r\nline 2\r\nline 3") == "line 1\nline 2\nline 3"


def test_cli_input_normalize_backslash_joins_lines() -> None:
    """Trailing backslash continuations join lines into one."""
    from coderai.ui.shell.prompt import normalize_multiline_input

    raw = "SELECT * \\\nFROM users \\\nWHERE id = 1"
    assert normalize_multiline_input(raw) == "SELECT * FROM users WHERE id = 1"


def test_cli_input_single_key_non_tty_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single-key read on non-TTY stdin returns empty without blocking."""
    from coderai.ui.shell.session_picker import _read_single_key

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert _read_single_key() == ""
