"""Unit tests for CoderAI CLI Terminal UI components and interactive UX."""

import pathlib
from unittest.mock import MagicMock

import pytest

from coderai.cli.app import _build_parser, describe_scope, get_scope_color
from coderai.cli.ascii_art import get_gradient_ascii_logo
from coderai.cli.diff_render import format_diff_text, render_diff_preview
from coderai.cli.exit_summary import compute_session_stats, render_exit_summary
from coderai.cli.file_mention import (
    _parse_line_range,
    expand_file_mentions,
    suggest_workspace_files,
)
from coderai.cli.interactive_menu import (
    render_skills_interactive,
    select_model_interactive,
    select_session_interactive,
)
from coderai.cli.plan_render import format_plan_content, parse_plan_stats, render_plan_preview
from coderai.cli.status_bar import format_status_bar, render_status_bar
from coderai.cli.thinking import render_thinking_block, summarize_thinking
from coderai.cli.tool_card import parse_tool_message, render_tool_card
from coderai.cli.welcome import render_welcome_screen
from coderai.core.session import SessionEntry, SessionManager, SessionMessage


def test_ascii_logo():
    logo = get_gradient_ascii_logo()
    assert logo is not None
    # If rich is present, it's a Text object, otherwise string
    assert len(str(logo)) > 0


def test_welcome_screen(tmp_path: pathlib.Path):
    # Should not raise exception
    render_welcome_screen(None, str(tmp_path), "gpt-4o", plan_mode=True, mcp_servers_count=2)
    mock_console = MagicMock()
    render_welcome_screen(
        mock_console, str(tmp_path), "gpt-4o", plan_mode=False, mcp_servers_count=0
    )
    assert mock_console.print.called


def test_diff_render():
    sample_diff = """--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,3 @@
-old_line
+new_line
 context_line
"""
    formatted = format_diff_text(sample_diff)
    assert formatted is not None

    # Render without console
    render_diff_preview(None, sample_diff, title="Test Diff")

    # Render with console
    mock_console = MagicMock()
    render_diff_preview(mock_console, sample_diff, title="Test Diff")
    assert mock_console.print.called


def test_plan_render():
    plan_text = """# Implementation Plan
- [x] Step 1: Initialize components
- [ ] Step 2: Build UI
- [x] Step 3: Run tests
"""
    total, completed = parse_plan_stats(plan_text)
    assert total == 3
    assert completed == 2

    formatted = format_plan_content(plan_text)
    assert formatted is not None

    mock_console = MagicMock()
    render_plan_preview(mock_console, plan_text, title="Task List")
    assert mock_console.print.called

    render_plan_preview(None, plan_text, title="Task List")


def test_thinking_hierarchy():
    long_trace = "I need to inspect the directory structure first. Then I will edit app.py to wire the new modules."
    summary = summarize_thinking(long_trace, max_chars=40)
    assert len(summary) <= 40
    assert summary.endswith("...")

    mock_console = MagicMock()
    render_thinking_block(mock_console, long_trace, elapsed_seconds=1.5, expanded=False)
    assert mock_console.print.called

    render_thinking_block(None, long_trace, elapsed_seconds=0.8, expanded=True)


def test_tool_card_and_summary():
    msg = SessionMessage(
        id="m1",
        session_id="s1",
        role="tool",
        content='{"name": "edit", "ok": true, "output": "Successfully updated", "metadata": {"diff_preview": "--- a\\n+++ b\\n+line"}}',
    )
    name, summary, ok, meta = parse_tool_message(msg)
    assert name == "edit"
    assert "Successfully updated" in summary
    assert ok is True
    assert meta and "diff_preview" in meta

    mock_console = MagicMock()
    render_tool_card(mock_console, msg)
    assert mock_console.print.called

    # Error message tool
    err_msg = SessionMessage(
        id="m2",
        session_id="s1",
        role="tool",
        content='{"name": "bash", "ok": false, "error": "Command not found"}',
    )
    render_tool_card(None, err_msg)


def test_status_bar():
    bar = format_status_bar("gpt-4o", 1250, True, "main")
    assert "gpt-4o" in str(bar)
    assert "1,250" in str(bar)
    assert "ON" in str(bar)
    assert "main" in str(bar)

    mock_console = MagicMock()
    render_status_bar(mock_console, "gpt-4o", 1250, False, "/tmp")
    assert mock_console.print.called

    render_status_bar(None, "gpt-4o", 0, True, "/tmp")


def test_file_mentions(tmp_path: pathlib.Path):
    (tmp_path / "hello.py").write_text("line1\nline2\nline3\nline4\nline5\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.txt").write_text("nested content\n")

    spec, s, e = _parse_line_range("hello.py:2-4")
    assert spec == "hello.py"
    assert s == 2 and e == 4

    spec, s, e = _parse_line_range("nested.txt:L3")
    assert spec == "nested.txt"
    assert s == 3 and e == 3

    prompt = "Please look at @hello.py:1-3 and explain @nested.txt"
    expanded, attached = expand_file_mentions(prompt, str(tmp_path))
    assert len(attached) == 2
    assert "Attached Context: hello.py" in expanded
    assert "Attached Context: sub/nested.txt" in expanded

    # Suggestions
    suggestions = suggest_workspace_files("nested", str(tmp_path))
    assert any("nested.txt" in s for s in suggestions)


def test_interactive_menus(monkeypatch):
    # Select model by number (1 -> gpt-5.6-sol, 2 -> gpt-5.6-terra, 4 -> gemini-3.7-flash)
    monkeypatch.setattr("builtins.input", lambda _: "1")
    chosen1 = select_model_interactive(None, "gpt-5.6-luna")
    assert chosen1 == "gpt-5.6-sol"

    monkeypatch.setattr("builtins.input", lambda _: "2")
    chosen2 = select_model_interactive(None, "gpt-5.6-luna")
    assert chosen2 == "gpt-5.6-terra"

    monkeypatch.setattr("builtins.input", lambda _: "4")
    chosen4 = select_model_interactive(None, "gpt-5.6-luna")
    assert chosen4 == "gemini-3.7-flash"

    # Select model by custom text
    monkeypatch.setattr("builtins.input", lambda _: "my-custom-model")
    chosen_custom = select_model_interactive(None, "gpt-4o")
    assert chosen_custom == "my-custom-model"

    # Rich console rendering of menu
    mock_console = MagicMock()
    monkeypatch.setattr("builtins.input", lambda _: "")
    chosen_keep = select_model_interactive(mock_console, "gpt-5.6-sol")
    assert chosen_keep == "gpt-5.6-sol"
    assert mock_console.print.called

    # Select session
    sessions = [
        SessionEntry(id="sess_1234567890", summary="Fix a bug in parser", active_tokens=450),
        SessionEntry(id="sess_abcdefghij", summary="Add new feature", active_tokens=890),
    ]
    monkeypatch.setattr("builtins.input", lambda _: "1")
    resumed = select_session_interactive(None, sessions)
    assert resumed == "sess_1234567890"

    # Cancel session select
    monkeypatch.setattr("builtins.input", lambda _: "")
    cancelled = select_session_interactive(None, sessions)
    assert cancelled is None

    # Render skills
    render_skills_interactive(None, "/tmp")
    mock_skills_console = MagicMock()
    render_skills_interactive(mock_skills_console, "/tmp")


def test_exit_summary(tmp_path: pathlib.Path):
    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": None, "model": "gpt-4o"},
        get_resolved_settings=lambda: {"model": "gpt-4o"},
    )
    # Empty stats
    stats = compute_session_stats(mgr, None)
    assert stats["turns"] == 0

    render_exit_summary(None, mgr, None)

    mock_console = MagicMock()
    render_exit_summary(mock_console, mgr, None)


def test_parser_and_scope_helpers():
    parser = _build_parser()
    args = parser.parse_args(["--plan", "--model", "o3-mini", "initial", "prompt"])
    assert args.plan is True
    assert args.model == "o3-mini"
    assert args.prompt == ["initial", "prompt"]

    assert describe_scope("read-in-cwd") == "reads inside this workspace"
    assert get_scope_color("read-in-cwd") == "green"
    assert get_scope_color("write-in-cwd") == "yellow"
    assert get_scope_color("write-out-cwd") == "red"


def test_ui_polish_enhancements(tmp_path: pathlib.Path):
    from coderai.cli.ascii_art import get_compact_gradient_badge
    from coderai.cli.diff_render import parse_diff_stats
    from coderai.cli.interactive_menu import estimate_model_cost, render_token_breakdown
    from coderai.cli.plan_render import make_plan_progress_bar
    from coderai.cli.statusline import make_mini_bar

    # 1. Compact badge
    badge = get_compact_gradient_badge()
    assert "CoderAI" in str(badge)

    # 2. Diff stats
    diff_text = """--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,4 @@
-line1
+line1_mod
+line2_added
 context
"""
    added, removed = parse_diff_stats(diff_text)
    assert added == 2
    assert removed == 1

    # 3. Plan progress bar
    bar = make_plan_progress_bar(3, 5, width=10)
    assert "60%" in bar
    assert "█" in bar

    # 4. Statusline mini bar
    mini = make_mini_bar(50.0, width=6)
    assert "■" in mini
    assert len(mini) == 6

    # 5. Cost estimation
    cost_gpt4o = estimate_model_cost("gpt-4o", 100_000, 20_000, 10_000)
    assert cost_gpt4o > 0.0
    cost_claude = estimate_model_cost("claude-3-7-sonnet", 50_000, 10_000)
    assert cost_claude > 0.0

    # 6. Token breakdown rendering
    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": None, "model": "gpt-4o"},
        get_resolved_settings=lambda: {"model": "gpt-4o"},
    )
    s_id = "test_sess_001"
    entry = SessionEntry(
        id=s_id,
        summary="Test cost rendering",
        active_tokens=1500,
        usage={"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500},
    )
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mgr, "get_session", lambda sid: entry)
    render_token_breakdown(None, mgr, s_id)
    mock_console = MagicMock()
    render_token_breakdown(mock_console, mgr, s_id)
    assert mock_console.print.called
    monkeypatch.undo()


def test_interactive_ctrl_c_and_exit(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    """Verify that pressing Ctrl+C at the prompt followed by /exit terminates cleanly with exit code 0."""
    from coderai.cli.app import _run_interactive

    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": None, "model": "gpt-4o"},
        get_resolved_settings=lambda: {"model": "gpt-4o"},
    )

    inputs = [KeyboardInterrupt(), "/exit"]

    def mock_read_user_turn(prompt=""):
        if not inputs:
            return "/exit"
        val = inputs.pop(0)
        if isinstance(val, BaseException):
            raise val
        return val

    monkeypatch.setattr("coderai.cli.app.read_user_turn", mock_read_user_turn)

    import asyncio

    ret = asyncio.run(_run_interactive(mgr, yes=False))
    assert ret == 0


def test_main_handles_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch):
    """Verify that main() catches KeyboardInterrupt / CancelledError and exits cleanly with code 0."""
    from unittest.mock import AsyncMock
    from coderai.cli.app import main

    async def mock_run_interactive(*args, **kwargs):
        raise KeyboardInterrupt()

    mock_mgr = MagicMock()
    mock_mgr.init_mcp_servers = AsyncMock()

    monkeypatch.setattr("coderai.cli.app._run_interactive", mock_run_interactive)
    monkeypatch.setattr("coderai.cli.app._build_manager", lambda *a, **kw: mock_mgr)

    ret = main([])
    assert ret == 0


@pytest.mark.asyncio
async def test_clear_task_cancellation():
    """Verify that _clear_task_cancellation clears any pending task cancellation."""
    import asyncio
    from coderai.cli.app import _clear_task_cancellation

    task = asyncio.current_task()
    assert task is not None
    task.cancel()
    assert task.cancelling() > 0

    _clear_task_cancellation()
    assert task.cancelling() == 0
