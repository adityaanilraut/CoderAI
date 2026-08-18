"""Unit tests for Phase 3: Rich TUI & Interactive Input Engine."""

from __future__ import annotations

import pathlib
from unittest.mock import MagicMock

import pytest

from coderai.cli.completer import CoderAICompleter
from coderai.cli.file_mention import suggest_workspace_files
from coderai.cli.fuzzy import fuzzy_filter, fuzzy_score
from coderai.cli.input_engine import (
    count_code_fences,
    is_multiline_incomplete,
    normalize_multiline_input,
    read_user_turn,
)
from coderai.cli.interactive_menu import select_model_interactive, select_session_interactive
from coderai.cli.status_bar import compute_token_gauge, format_status_bar, render_status_bar
from coderai.core.session import SessionEntry


# ============================================================================
# 1. Fuzzy Matching & Ranking Engine (fuzzy.py)
# ============================================================================


def test_fuzzy_score_exact_and_prefix():
    matched, score_exact = fuzzy_score("compact", "compact")
    assert matched is True
    assert score_exact >= 10000

    matched, score_prefix = fuzzy_score("comp", "compact")
    assert matched is True
    assert score_prefix > 2000

    matched, score_none = fuzzy_score("xyz", "compact")
    assert matched is False
    assert score_none == 0


def test_fuzzy_score_subsequence_and_separators():
    matched, score_subseq = fuzzy_score("capp", "coderai/cli/app.py")
    assert matched is True
    assert score_subseq > 0

    # Word boundary bonus matches higher than random non-boundary subsequence
    _, score_boundary = fuzzy_score("ca", "coderai_app")
    _, score_middle = fuzzy_score("ca", "xxcxxax")
    assert score_boundary > score_middle


def test_fuzzy_filter_ranking():
    commands = ["/compact", "/config", "/continue", "/clear", "/undo", "/sessions"]

    filtered = fuzzy_filter("/cp", commands)
    assert "/compact" in filtered
    assert filtered[0] == "/compact"

    filtered_und = fuzzy_filter("/und", commands)
    assert filtered_und == ["/undo"]

    # Key function filtering
    items = [{"name": "app.py", "type": "code"}, {"name": "test_core.py", "type": "test"}]
    res = fuzzy_filter("test", items, key_func=lambda x: x["name"])
    assert len(res) == 1
    assert res[0]["name"] == "test_core.py"


# ============================================================================
# 2. Multi-line & Advanced Input Buffering Engine (input_engine.py)
# ============================================================================


def test_count_code_fences():
    text_none = "print('hello')"
    assert count_code_fences(text_none) == 0

    text_single = "```python\nprint('hello')\n"
    assert count_code_fences(text_single) == 1

    text_pair = "```python\nprint('hello')\n```"
    assert count_code_fences(text_pair) == 2


def test_is_multiline_incomplete():
    # Empty or single line
    assert not is_multiline_incomplete([])
    assert not is_multiline_incomplete(["Hello world"])

    # Trailing backslash
    assert is_multiline_incomplete(["line 1 \\"])
    assert not is_multiline_incomplete(["escaped backslash \\\\"])

    # Unclosed code fence
    assert is_multiline_incomplete(["```python", "def foo():"])
    assert not is_multiline_incomplete(["```python", "def foo():", "```"])


def test_normalize_multiline_input():
    crlf_text = "line 1\r\nline 2\r\nline 3"
    assert normalize_multiline_input(crlf_text) == "line 1\nline 2\nline 3"

    # Line continuations
    continuation_text = "git commit \\\n-m 'Initial commit' \\\n--amend"
    normalized = normalize_multiline_input(continuation_text)
    assert normalized == "git commit -m 'Initial commit' --amend"


def test_read_user_turn_single_line():
    simulated_inputs = ["Hello CoderAI"]
    result = read_user_turn(input_func=lambda _: simulated_inputs.pop(0))
    assert result == "Hello CoderAI"


def test_read_user_turn_multiline_code_block():
    simulated_inputs = [
        "```python",
        "def add(a, b):",
        "    return a + b",
        "```",
    ]
    result = read_user_turn(input_func=lambda _: simulated_inputs.pop(0))
    assert "def add(a, b):" in result
    assert result.startswith("```python")
    assert result.endswith("```")


def test_read_user_turn_multiline_backslash():
    simulated_inputs = [
        "pytest \\",
        "-v \\",
        "tests/test_core.py",
    ]
    result = read_user_turn(input_func=lambda _: simulated_inputs.pop(0))
    assert result == "pytest -v tests/test_core.py"


# ============================================================================
# 3. Autocompletion & Fuzzy File Mentions (completer.py & file_mention.py)
# ============================================================================


def test_fuzzy_workspace_file_suggestions(tmp_path: pathlib.Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "controller.py").write_text("class Controller: pass")
    (tmp_path / "src" / "config_loader.py").write_text("def load(): pass")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_controller.py").write_text("def test_c(): pass")

    # Fuzzy query matches
    suggestions = suggest_workspace_files("ctrl", str(tmp_path))
    assert any("controller.py" in s for s in suggestions)

    suggestions_cfg = suggest_workspace_files("cfgl", str(tmp_path))
    assert any("config_loader.py" in s for s in suggestions_cfg)


def test_completer_fuzzy_slash_commands(tmp_path: pathlib.Path):
    completer = CoderAICompleter(str(tmp_path), lambda: "gpt-4o")

    # Fuzzy slash command completion
    first_opt = completer.complete("/cpt", 0)
    assert first_opt is not None
    assert "/compact" in first_opt

    undo_opt = completer.complete("/und", 0)
    assert undo_opt is not None
    assert "/undo" in undo_opt

    # Subcommand completion for /mcp
    mcp_sub = completer.complete("/mcp rec", 0)
    assert mcp_sub == "reconnect"

    # Subcommand completion for /thinking
    think_sub = completer.complete("/thinking sum", 0)
    assert think_sub == "summary"


# ============================================================================
# 4. Dynamic Status Bar Context Window Gauges (status_bar.py)
# ============================================================================


def test_compute_token_gauge_percentages_and_colors():
    # gpt-4o has 256k default context window (262,144)
    display_low, style_low, pct_low = compute_token_gauge(1310, "gpt-4o")
    assert "1,310" in display_low
    assert "256k" in display_low
    assert pct_low == pytest.approx(0.5, 0.1)
    assert style_low == "green"

    # ~70% of 256k -> yellow
    display_mid, style_mid, pct_mid = compute_token_gauge(180000, "gpt-4o")
    assert "180,000" in display_mid
    assert pct_mid >= 60 and pct_mid < 80
    assert style_mid == "yellow"

    # ~90% of 256k -> red
    display_high, style_high, pct_high = compute_token_gauge(240000, "gpt-4o")
    assert "240,000" in display_high
    assert pct_high > 80
    assert style_high == "bold red"


def test_format_status_bar_dynamic_gauges():
    bar = format_status_bar("deepseek-v4-pro", 52428, True, "main", turns=3, mcp_count=2)
    bar_str = str(bar)
    assert "deepseek-v4-pro" in bar_str
    assert "52,428" in bar_str
    assert "1M" in bar_str  # deepseek-v4-pro default context window is 1M
    assert "Turns: 3" in bar_str
    assert "MCP: 2" in bar_str


def test_render_status_bar_executes_without_error(tmp_path: pathlib.Path):
    mock_console = MagicMock()
    render_status_bar(mock_console, "gpt-4o", 2500, False, str(tmp_path), turns=1, mcp_count=1)
    assert mock_console.print.called

    # Plain text fallback
    render_status_bar(None, "gpt-4o", 2500, True, str(tmp_path))


# ============================================================================
# 5. Interactive Selectors with Fuzzy Filtering (interactive_menu.py)
# ============================================================================


def test_select_model_fuzzy_search(monkeypatch: pytest.MonkeyPatch):
    # Fuzzy match 'sonnet' -> 'claude-3-7-sonnet'
    monkeypatch.setattr("builtins.input", lambda _: "sonnet")
    chosen = select_model_interactive(None, "gpt-4o")
    assert "sonnet" in chosen.lower()

    # Fuzzy match 'deepseek' -> 'deepseek-v4-pro'
    monkeypatch.setattr("builtins.input", lambda _: "dpro")
    chosen_ds = select_model_interactive(None, "gpt-4o")
    assert "deepseek" in chosen_ds.lower()


def test_select_session_fuzzy_search(monkeypatch: pytest.MonkeyPatch):
    sessions = [
        SessionEntry(
            id="sess_auth_handler", summary="Implement JWT authentication flow", active_tokens=1200
        ),
        SessionEntry(
            id="sess_db_migration",
            summary="Add PostgreSQL migrations for users",
            active_tokens=3400,
        ),
    ]

    # Search by summary keywords
    monkeypatch.setattr("builtins.input", lambda _: "jwt")
    selected_auth = select_session_interactive(None, sessions)
    assert selected_auth == "sess_auth_handler"

    monkeypatch.setattr("builtins.input", lambda _: "postgres")
    selected_db = select_session_interactive(None, sessions)
    assert selected_db == "sess_db_migration"
