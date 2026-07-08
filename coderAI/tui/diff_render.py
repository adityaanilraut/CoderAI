"""Unified diff rendering for the TUI with gutter columns."""

from __future__ import annotations

import difflib
import re
from typing import Dict, List, Optional, Tuple

from rich.markup import escape

from coderAI.tui.theme import Styles, Tokens

DIFF_MAX_LINES = 12

# Word-level emphasis: tokens are runs of whitespace, word chars, or other
# symbols so SequenceMatcher diffs at word granularity instead of chars.
_WORD_RE = re.compile(r"\s+|\w+|[^\w\s]+")

# Below this token-level similarity a paired −/+ line is treated as a full
# rewrite: emphasizing nearly everything is worse than emphasizing nothing.
_EMPH_MIN_RATIO = 0.4

Span = Tuple[int, int]


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


def _word_spans(old: str, new: str) -> Optional[Tuple[List[Span], List[Span]]]:
    """Char-offset spans that differ between a paired −/+ line.

    Returns (old_spans, new_spans), or None when the lines are too dissimilar
    for word-level emphasis to carry meaning (whole-line coloring reads better).
    """
    if not old.strip() or not new.strip():
        return None
    old_tokens = _WORD_RE.findall(old)
    new_tokens = _WORD_RE.findall(new)
    matcher = difflib.SequenceMatcher(None, old_tokens, new_tokens, autojunk=False)
    if matcher.ratio() < _EMPH_MIN_RATIO:
        return None

    def offsets(tokens: List[str]) -> List[int]:
        out = [0]
        for tok in tokens:
            out.append(out[-1] + len(tok))
        return out

    old_off = offsets(old_tokens)
    new_off = offsets(new_tokens)
    old_spans: List[Span] = []
    new_spans: List[Span] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if i2 > i1:
            old_spans.append((old_off[i1], old_off[i2]))
        if j2 > j1:
            new_spans.append((new_off[j1], new_off[j2]))
    return old_spans, new_spans


def _paired_emphasis(window: List[Tuple[str, str]]) -> Dict[int, List[Span]]:
    """Word-diff spans per window index, for del-runs followed by add-runs.

    The i-th deleted line of a run is paired with the i-th added line — the
    common shape of an edit — and unpaired leftovers keep whole-line styling.
    """
    emphasis: Dict[int, List[Span]] = {}
    i = 0
    while i < len(window):
        if window[i][0] != "del":
            i += 1
            continue
        j = i
        while j < len(window) and window[j][0] == "del":
            j += 1
        k = j
        while k < len(window) and window[k][0] == "add":
            k += 1
        for off in range(min(j - i, k - j)):
            spans = _word_spans(window[i + off][1], window[j + off][1])
            if spans is not None:
                old_spans, new_spans = spans
                if old_spans:
                    emphasis[i + off] = old_spans
                if new_spans:
                    emphasis[j + off] = new_spans
        i = k
    return emphasis


def _emphasized_body(text: str, spans: List[Span], base_style: str, emph_style: str) -> str:
    """Markup for a diff line body with emphasized spans over a base style."""
    if not spans:
        return f"[{base_style}]{escape(text)}[/]"
    parts: List[str] = []
    pos = 0
    for start, end in spans:
        if start > pos:
            parts.append(f"[{base_style}]{escape(text[pos:start])}[/]")
        parts.append(f"[{emph_style}]{escape(text[start:end])}[/]")
        pos = end
    if pos < len(text):
        parts.append(f"[{base_style}]{escape(text[pos:])}[/]")
    return "".join(parts)


def format_diff_gutter(diff: str, max_lines: int = DIFF_MAX_LINES) -> str:
    """Render a unified diff with monospace gutter: line numbers, +/- prefix, colored backgrounds.

    Paired −/+ lines additionally get word-level emphasis on the changed spans.
    Returns a Rich markup string suitable for Static or RichLog.
    """
    parsed = parse_unified_diff(diff)
    if not parsed:
        return ""

    lines_out: List[str] = []
    window = _window_lines(parsed, max_lines)
    emphasis = _paired_emphasis(window)

    # Build gutter output
    for idx, (kind, text) in enumerate(window):
        if kind == "meta":
            lines_out.append(f"[{Tokens.TEXT_DIM}]{escape(text)}[/]")
        elif kind == "hunk":
            lines_out.append(f"[{Tokens.TEXT_MUTED}]{escape(text)}[/]")
        elif kind == "add":
            body = _emphasized_body(
                text, emphasis.get(idx, []), Styles.GUTTER_ADD, Styles.DIFF_ADD_EMPH
            )
            lines_out.append(
                f"[on {Styles.DIFF_ADD_BG}]"
                f"[{Styles.GUTTER_LINE}]    [/]"
                f" [{Styles.GUTTER_ADD}]+[/] "
                f"{body}"
                f"[/]"
            )
        elif kind == "del":
            body = _emphasized_body(
                text, emphasis.get(idx, []), Styles.GUTTER_REMOVE, Styles.DIFF_REMOVE_EMPH
            )
            lines_out.append(
                f"[on {Styles.DIFF_REMOVE_BG}]"
                f"[{Styles.GUTTER_LINE}]    [/]"
                f" [{Styles.GUTTER_REMOVE}]−[/] "
                f"{body}"
                f"[/]"
            )
        elif kind == "ctx":
            lines_out.append(
                f"[{Styles.GUTTER_LINE}]    [/]"
                f" [{Styles.GUTTER_CTX}] [/] "
                f"[{Styles.GUTTER_CTX}]{escape(text)}[/]"
            )

    return "\n".join(lines_out)
