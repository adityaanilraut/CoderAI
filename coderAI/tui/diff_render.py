"""Unified diff rendering for the TUI."""

from __future__ import annotations

from typing import List, Tuple


def parse_unified_diff(diff: str) -> List[Tuple[str, str]]:
    """Return (line_type, text) where line_type is add|del|ctx|hunk|meta."""
    lines: List[Tuple[str, str]] = []
    for raw in diff.splitlines():
        if raw.startswith("@@"):
            lines.append(("hunk", raw))
        elif raw.startswith("+++") or raw.startswith("---"):
            lines.append(("meta", raw))
        elif raw.startswith("+"):
            lines.append(("add", raw))
        elif raw.startswith("-"):
            lines.append(("del", raw))
        else:
            lines.append(("ctx", raw))
    return lines


def format_diff_compact(diff: str, max_lines: int = 12) -> str:
    parsed = parse_unified_diff(diff)
    if len(parsed) <= max_lines:
        return "\n".join(t for _, t in parsed)
    head = parsed[: max_lines // 2]
    tail = parsed[-(max_lines // 2) :]
    omitted = len(parsed) - len(head) - len(tail)
    mid = [("ctx", f"… ({omitted} lines elided) …")]
    return "\n".join(t for _, t in head + mid + tail)
