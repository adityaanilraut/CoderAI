"""Tests for Phase 1 critical CLI/TUI bug fixes."""

from __future__ import annotations

import io
import sys
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from coderai.cli.input_engine import (
    count_code_fences,
    count_triple_quotes,
    is_multiline_incomplete,
    normalize_multiline_input,
)
from coderai.cli.interactive_menu import _read_single_key, select_with_arrows


# ==========================================
# 1. Tests for Multiline Normalization
# ==========================================


def test_normalize_multiline_input_crlf():
    """Verify CRLF line endings are cleanly normalized to LF."""
    raw = "line 1\r\nline 2\r\nline 3"
    assert normalize_multiline_input(raw) == "line 1\nline 2\nline 3"


def test_normalize_multiline_input_backslash_continuation():
    """Verify odd trailing backslashes join lines and even backslashes are preserved."""
    # Single backslash continuation
    raw = "SELECT * \\\nFROM users \\\nWHERE id = 1"
    assert normalize_multiline_input(raw) == "SELECT * FROM users WHERE id = 1"

    # Double backslash (escaped) is preserved and does not join
    raw_escaped = "path\\\\\\nnext line"
    assert "path\\\\" in normalize_multiline_input(raw_escaped)


def test_normalize_multiline_input_envelope_unwrapping():
    """Verify clean delimiter envelopes are unwrapped while internal quotes are preserved."""
    # Clean envelope with exactly 2 triple quotes
    envelope = '"""\ndef calculate_sum(a, b):\n    return a + b\n"""'
    assert normalize_multiline_input(envelope) == "def calculate_sum(a, b):\n    return a + b"

    # Single-quote envelope
    single_envelope = "'''\nSELECT 1;\n'''"
    assert normalize_multiline_input(single_envelope) == "SELECT 1;"

    # Multiple triple quotes in the same prompt must NOT be corrupted
    multiple_quotes = '"""first block""" and """second block"""'
    assert normalize_multiline_input(multiple_quotes) == '"""first block""" and """second block"""'

    # Docstring inside function must be preserved
    docstring_prompt = 'def foo():\n    """Internal docstring."""\n    return True'
    assert normalize_multiline_input(docstring_prompt) == docstring_prompt


# ==========================================
# 2. Tests for _read_single_key
# ==========================================


def test_read_single_key_non_tty(monkeypatch: pytest.MonkeyPatch):
    """Verify non-TTY stdin returns empty string immediately."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert _read_single_key() == ""


def test_read_single_key_escape_timeout(monkeypatch: pytest.MonkeyPatch):
    """Verify standalone escape key triggers select timeout and returns ESCAPE without blocking."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdin.fileno", lambda: 0)

    # Mock termios and tty
    mock_termios = MagicMock()
    mock_tty = MagicMock()
    monkeypatch.setitem(sys.modules, "termios", mock_termios)
    monkeypatch.setitem(sys.modules, "tty", mock_tty)

    # Mock stdin.read(1) returning '\x1b'
    monkeypatch.setattr("sys.stdin.read", lambda _: "\x1b")
    # Mock select returning empty (timeout expired)
    mock_select = MagicMock(return_value=([], [], []))
    monkeypatch.setattr("select.select", mock_select)

    assert _read_single_key() == "ESCAPE"


def test_read_single_key_arrow_sequences(monkeypatch: pytest.MonkeyPatch):
    """Verify ANSI escape sequences parse correctly for arrows and navigation keys."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdin.fileno", lambda: 0)

    mock_termios = MagicMock()
    mock_tty = MagicMock()
    monkeypatch.setitem(sys.modules, "termios", mock_termios)
    monkeypatch.setitem(sys.modules, "tty", mock_tty)

    # Mock select to return ready stream
    monkeypatch.setattr("select.select", lambda *args, **kwargs: ([sys.stdin], [], []))

    sequences = {
        ("\x1b", "[", "A"): "UP",
        ("\x1b", "[", "B"): "DOWN",
        ("\x1b", "[", "C"): "RIGHT",
        ("\x1b", "[", "D"): "LEFT",
        ("\x1b", "[", "H"): "HOME",
        ("\x1b", "[", "F"): "END",
        ("\x1b", "[", "Z"): "BACKTAB",
    }

    for seq, expected in sequences.items():
        chars = iter(seq)
        monkeypatch.setattr("sys.stdin.read", lambda _, it=chars: next(it))
        assert _read_single_key() == expected


def test_read_single_key_extended_sequences(monkeypatch: pytest.MonkeyPatch):
    """Verify PageUp, PageDown, Delete sequences parse correctly."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdin.fileno", lambda: 0)

    mock_termios = MagicMock()
    mock_tty = MagicMock()
    monkeypatch.setitem(sys.modules, "termios", mock_termios)
    monkeypatch.setitem(sys.modules, "tty", mock_tty)
    monkeypatch.setattr("select.select", lambda *args, **kwargs: ([sys.stdin], [], []))

    extended = {
        ("\x1b", "[", "3", "~"): "DELETE",
        ("\x1b", "[", "5", "~"): "PAGE_UP",
        ("\x1b", "[", "6", "~"): "PAGE_DOWN",
    }

    for seq, expected in extended.items():
        chars = iter(seq)
        monkeypatch.setattr("sys.stdin.read", lambda _, it=chars: next(it))
        assert _read_single_key() == expected


def test_read_single_key_windows(monkeypatch: pytest.MonkeyPatch):
    """Verify Windows msvcrt reading for arrows and control keys."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("os.name", "nt")

    mock_msvcrt = MagicMock()
    monkeypatch.setitem(sys.modules, "msvcrt", mock_msvcrt)

    # Test Enter
    mock_msvcrt.getch.side_effect = [b"\r"]
    assert _read_single_key() == "ENTER"

    # Test Up arrow (\xe0 + H)
    mock_msvcrt.getch.side_effect = [b"\xe0", b"H"]
    assert _read_single_key() == "UP"

    # Test Down arrow (\xe0 + P)
    mock_msvcrt.getch.side_effect = [b"\xe0", b"P"]
    assert _read_single_key() == "DOWN"


# ==========================================
# 3. Tests for select_with_arrows Navigation & Search
# ==========================================


def test_select_with_arrows_search_filtering_with_letters(monkeypatch: pytest.MonkeyPatch):
    """Verify typing letters containing j, k, q (e.g. 'flask') filters the menu without hijacking cursor."""
    console = Console(file=io.StringIO())
    items = [
        ("django", "Django", "Python web framework"),
        ("flask", "Flask", "Lightweight WSGI web framework"),
        ("fastapi", "FastAPI", "Modern async web framework"),
    ]

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    # Sequence of keypresses: 'f', 'l', 'a', 's', 'k', 'ENTER'
    keys = iter(["f", "l", "a", "s", "k", "ENTER"])
    monkeypatch.setattr("coderai.cli.interactive_menu._read_single_key", lambda: next(keys))

    res = select_with_arrows(console, items, title="Select Framework", default_idx=0)
    # The filtered result should match 'flask' (index 1 in original items)
    assert res == 1


def test_select_with_arrows_search_filtering_with_digits(monkeypatch: pytest.MonkeyPatch):
    """Verify typing digits like '4' or '32' filters model names rather than immediately selecting item #4."""
    console = Console(file=io.StringIO())
    items = [
        ("gpt-3.5", "GPT-3.5 Turbo", "Legacy OpenAI model"),
        ("claude-3-opus", "Claude 3 Opus", "Anthropic flagship model"),
        ("qwen-32b", "Qwen 2.5 Coder 32B", "Open source coding model"),
        ("gpt-4o", "GPT-4o", "OpenAI flagship model"),
    ]

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    # Type '4', 'o', 'ENTER' -> should filter to 'gpt-4o' (index 3) and select it
    keys = iter(["4", "o", "ENTER"])
    monkeypatch.setattr("coderai.cli.interactive_menu._read_single_key", lambda: next(keys))

    res = select_with_arrows(console, items, title="Select Model", default_idx=0)
    assert res == 3


def test_select_with_arrows_escape_clears_filter_first(monkeypatch: pytest.MonkeyPatch):
    """Verify pressing Escape when filter_query is active clears search filter instead of aborting menu."""
    console = Console(file=io.StringIO())
    items = [
        ("item_a", "Item Alpha", "First item"),
        ("item_b", "Item Beta", "Second item"),
        ("item_c", "Item Gamma", "Third item"),
    ]

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    # Type 'b', 'e', 't', 'a' (highlights Item Beta), then 'ESCAPE' (clears filter, keeps Item Beta highlighted),
    # then 'DOWN' (navigates to Item Gamma), then 'ENTER'
    keys = iter(["b", "e", "t", "a", "ESCAPE", "DOWN", "ENTER"])
    monkeypatch.setattr("coderai.cli.interactive_menu._read_single_key", lambda: next(keys))

    res = select_with_arrows(console, items, title="Test Menu", default_idx=0)
    assert res == 2


def test_select_with_arrows_page_navigation(monkeypatch: pytest.MonkeyPatch):
    """Verify PAGE_DOWN and PAGE_UP navigate in chunks."""
    console = Console(file=io.StringIO())
    items = [(f"opt_{i}", f"Option {i}", f"Description {i}") for i in range(20)]

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    # PAGE_DOWN jumps +5, then ENTER selects option index 5
    keys = iter(["PAGE_DOWN", "ENTER"])
    monkeypatch.setattr("coderai.cli.interactive_menu._read_single_key", lambda: next(keys))

    res = select_with_arrows(console, items, title="Long Menu", default_idx=0)
    assert res == 5
