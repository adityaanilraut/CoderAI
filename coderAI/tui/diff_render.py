"""Unified diff rendering for the TUI with gutter columns."""

from __future__ import annotations

from typing import List, Tuple

from rich.markup import escape

from coderAI.tui.theme import Styles, Tokens

DIFF_MAX_LINES = 12


def _window_lines(parsed, max_lines):
    """Return a windowed view of parsed diff lines with an ellipsis if truncated."""
    if len(parsed) <= max_lines:
        return parsed
    head = parsed[: max_lines // 2]
    tail = parsed[-(max_lines // 2) :]
    omitted = len(parsed) - len(head) - len(tail)
    mid = [("ctx", f"… ({omitted} lines elided) …")]
    return head + mid + tail


def parse_unified_diff(diff: str) -> List[Tuple[str, str]]:
    """Return (line_type, text) where line_type is add|del|ctx|hunk|meta."""
    lines: List[Tuple[str, str]] = []
    for raw in diff.splitlines():
        if raw.startswith("@@"):
            lines.append(("hunk", raw))
        elif raw.startswith("+++") or raw.startswith("---"):
            lines.append(("meta", raw))
        elif raw.startswith("+"):
            lines.append(("add", raw[1:]))
        elif raw.startswith("-"):
            lines.append(("del", raw[1:]))
        else:
            lines.append(("ctx", raw[1:] if raw.startswith(" ") else raw))
    return lines


def format_diff_compact(diff: str, max_lines: int = DIFF_MAX_LINES) -> str:
    parsed = parse_unified_diff(diff)
    windowed = _window_lines(parsed, max_lines)
    return "\n".join(t for _, t in windowed)


def format_diff_gutter(diff: str, max_lines: int = DIFF_MAX_LINES) -> str:
    """Render a unified diff with monospace gutter: line numbers, +/- prefix, colored backgrounds.

    Returns a Rich markup string suitable for Static or RichLog.
    """
    parsed = parse_unified_diff(diff)
    if not parsed:
        return ""

    lines_out: List[str] = []
    window = _window_lines(parsed, max_lines)

    # Build gutter output
    for kind, text in window:
        if kind == "meta":
            lines_out.append(f"[{Tokens.TEXT_DIM}]{escape(text)}[/]")
        elif kind == "hunk":
            lines_out.append(f"[{Tokens.TEXT_MUTED}]{escape(text)}[/]")
        elif kind == "add":
            lines_out.append(
                f"[{Styles.DIFF_ADD_BG}]"
                f"[{Styles.GUTTER_LINE}]    [/]"
                f" [{Styles.GUTTER_ADD}]+[/] "
                f"[{Styles.GUTTER_ADD}]{escape(text)}[/]"
                f"[/]"
            )
        elif kind == "del":
            lines_out.append(
                f"[{Styles.DIFF_REMOVE_BG}]"
                f"[{Styles.GUTTER_LINE}]    [/]"
                f" [{Styles.GUTTER_REMOVE}]\u2212[/] "
                f"[{Styles.GUTTER_REMOVE}]{escape(text)}[/]"
                f"[/]"
            )
        elif kind == "ctx":
            lines_out.append(
                f"[{Styles.GUTTER_LINE}]    [/]"
                f" [{Styles.GUTTER_CTX}] [/] "
                f"[{Styles.GUTTER_CTX}]{escape(text)}[/]"
            )

    return "\n".join(lines_out)
