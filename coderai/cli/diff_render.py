"""Unified diff renderer — enhanced parity with Kimi CLI utils/rich/diff_render.py.

Provides:
- parse_diff_stats (legacy unified diff string)
- format_diff_text (legacy)
- render_diff_preview (legacy string path, now with enhanced styling)
- collect_diff_hunks / _build_diff_lines (structured path via SequenceMatcher)
- render_diff_panel (full Panel/Table with line numbers, background colors, syntax highlight, inline diff)
- render_diff_preview_structured (changed-lines-only preview)
- render_diff_summary_panel (large file summary)

Pure CLI, no browser.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum, auto
from typing import Any

from rich.console import Console, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ---------------------------------------------------------------------------
# Legacy unified-string helpers (kept for backward compat)
# ---------------------------------------------------------------------------


def parse_diff_stats(diff_text: str) -> tuple[int, int]:
    """Parse count of added and removed lines from unified diff."""
    added = 0
    removed = 0
    for line in diff_text.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def format_diff_text(diff_text: str) -> Text:
    """Format a unified diff string into a syntax-highlighted Rich Text object."""
    formatted = Text()
    lines = diff_text.splitlines()
    for line in lines:
        if line.startswith("--- ") or line.startswith("+++ "):
            formatted.append(f"{line}\n", style="bold cyan")
        elif line.startswith("@@"):
            formatted.append(f"{line}\n", style="bold magenta")
        elif line.startswith("+"):
            formatted.append(f"{line}\n", style="green")
        elif line.startswith("-"):
            formatted.append(f"{line}\n", style="red")
        elif line.startswith("\\"):
            formatted.append(f"{line}\n", style="dim italic")
        else:
            formatted.append(f"{line}\n", style="dim")
    return formatted


# ---------------------------------------------------------------------------
# Structured diff model (ported from Kimi)
# ---------------------------------------------------------------------------

MAX_PREVIEW_CHANGED_LINES = 6
_INLINE_DIFF_MIN_RATIO = 0.5


class DiffLineKind(Enum):
    CONTEXT = auto()
    ADD = auto()
    DELETE = auto()


@dataclass(slots=True)
class DiffLine:
    kind: DiffLineKind
    old_num: int
    new_num: int
    code: str
    content: Text | None = None
    is_inline_paired: bool = False


@dataclass(slots=True)
class DiffHunk:
    lines: list[DiffLine]


def _build_diff_lines(
    old_text: str,
    new_text: str,
    old_start: int,
    new_start: int,
    n_context: int = 3,
) -> list[list[DiffLine]]:
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    matcher = SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    hunks: list[list[DiffLine]] = []
    for group in matcher.get_grouped_opcodes(n=n_context):
        hunk: list[DiffLine] = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for k in range(i2 - i1):
                    hunk.append(
                        DiffLine(
                            DiffLineKind.CONTEXT,
                            old_start + i1 + k,
                            new_start + j1 + k,
                            old_lines[i1 + k],
                        )
                    )
            elif tag == "delete":
                for k in range(i2 - i1):
                    hunk.append(
                        DiffLine(DiffLineKind.DELETE, old_start + i1 + k, 0, old_lines[i1 + k])
                    )
            elif tag == "insert":
                for k in range(j2 - j1):
                    hunk.append(
                        DiffLine(DiffLineKind.ADD, 0, new_start + j1 + k, new_lines[j1 + k])
                    )
            elif tag == "replace":
                for k in range(i2 - i1):
                    hunk.append(
                        DiffLine(DiffLineKind.DELETE, old_start + i1 + k, 0, old_lines[i1 + k])
                    )
                for k in range(j2 - j1):
                    hunk.append(
                        DiffLine(DiffLineKind.ADD, 0, new_start + j1 + k, new_lines[j1 + k])
                    )
        if hunk:
            hunks.append(hunk)
    return hunks


def _make_highlighter(path: str):  # type: ignore[no-untyped-def]
    ext = path.rsplit(".", 1)[-1] if "." in path else ""
    try:
        from coderai.cli.syntax_theme import KimiSyntax

        return KimiSyntax("", ext if ext else "text")
    except Exception:
        from rich.syntax import Syntax

        return Syntax("", ext if ext else "text")


def _highlight(highlighter: Any, code: str) -> Text:
    try:
        t = highlighter.highlight(code)  # type: ignore[union-attr]
        if t.plain.endswith("\n"):
            t.right_crop(1)
        return t
    except Exception:
        return Text(code)


def _build_offset_map(raw: str, rendered: str, tab_size: int) -> list[int]:
    if raw == rendered:
        return list(range(len(raw) + 1))
    offsets: list[int] = []
    col = 0
    for ch in raw:
        offsets.append(col)
        if ch == "\t":
            col += tab_size - (col % tab_size)
        else:
            col += 1
    offsets.append(col)
    if col != len(rendered):
        rendered_len = len(rendered)
        raw_len = len(raw)
        if raw_len == 0:
            return [rendered_len]
        return [(i * rendered_len) // raw_len for i in range(raw_len)] + [rendered_len]
    return offsets


def _apply_inline_diff(
    highlighter: Any, del_lines: list[DiffLine], add_lines: list[DiffLine]
) -> None:
    try:
        from coderai.cli.theme import get_diff_colors

        colors = get_diff_colors()
    except Exception:
        return
    tab_size = getattr(highlighter, "tab_size", 4)
    paired = min(len(del_lines), len(add_lines))
    for j in range(paired):
        old_code = del_lines[j].code
        new_code = add_lines[j].code
        old_text = _highlight(highlighter, old_code)
        new_text = _highlight(highlighter, new_code)
        del_lines[j].content = old_text
        add_lines[j].content = new_text
        sm = SequenceMatcher(None, old_code, new_code)
        if sm.ratio() < _INLINE_DIFF_MIN_RATIO:
            continue
        old_map = _build_offset_map(old_code, old_text.plain, tab_size)
        new_map = _build_offset_map(new_code, new_text.plain, tab_size)
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op in ("delete", "replace"):
                try:
                    old_text.stylize(colors.del_hl, old_map[i1], old_map[i2])
                except Exception:
                    pass
            if op in ("insert", "replace"):
                try:
                    new_text.stylize(colors.add_hl, new_map[j1], new_map[j2])
                except Exception:
                    pass
        del_lines[j].content = old_text
        del_lines[j].is_inline_paired = True
        add_lines[j].content = new_text
        add_lines[j].is_inline_paired = True


def _highlight_hunk(highlighter: Any, hunk: list[DiffLine]) -> None:
    i = 0
    while i < len(hunk):
        if hunk[i].kind == DiffLineKind.DELETE:
            del_start = i
            while i < len(hunk) and hunk[i].kind == DiffLineKind.DELETE:
                i += 1
            add_start = i
            while i < len(hunk) and hunk[i].kind == DiffLineKind.ADD:
                i += 1
            _apply_inline_diff(highlighter, hunk[del_start:add_start], hunk[add_start:i])
        else:
            i += 1
    for dl in hunk:
        if dl.content is None:
            dl.content = _highlight(highlighter, dl.code)


def _build_diff_header(path: str, added: int, removed: int) -> Text:
    header = Text()
    if added > 0 and removed == 0:
        header.append("[CREATED] ", style="bold green")
    elif added == 0 and removed > 0:
        header.append("[DELETED] ", style="bold red")
    elif added > 0 or removed > 0:
        header.append("[MODIFIED] ", style="bold yellow")

    header.append(path, style="bold white")
    if added > 0 or removed > 0:
        header.append(" (", style="dim")
        if added > 0:
            header.append(f"+{added}", style="bold green")
        if added > 0 and removed > 0:
            header.append(" ", style="dim")
        if removed > 0:
            header.append(f"-{removed}", style="bold red")
        header.append(")", style="dim")
    return header


# ---------------------------------------------------------------------------
# Public: collect hunks from text pair or diff string
# ---------------------------------------------------------------------------


def collect_diff_hunks_from_texts(
    old_text: str,
    new_text: str,
    old_start: int = 1,
    new_start: int = 1,
) -> tuple[list[list[DiffLine]], int, int]:
    hunks = _build_diff_lines(old_text, new_text, old_start, new_start)
    added = sum(1 for h in hunks for dl in h if dl.kind == DiffLineKind.ADD)
    removed = sum(1 for h in hunks for dl in h if dl.kind == DiffLineKind.DELETE)
    return hunks, added, removed


def parse_unified_diff_to_hunks(diff_text: str) -> tuple[list[list[DiffLine]], int, int, str]:
    """Best-effort: derive a file path and hunks from a unified diff string.

    For simple single-file diffs without headers, falls back to treating
    '+'/'-' lines as add/delete without line numbers.
    """
    path = "diff"
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip().lstrip("a/").lstrip("b/")
            break
    # If no old/new_text split, synthesize from +/- lines
    added = sum(
        1
        for line_item in diff_text.splitlines()
        if line_item.startswith("+") and not line_item.startswith("+++")
    )
    removed = sum(
        1
        for line_item in diff_text.splitlines()
        if line_item.startswith("-") and not line_item.startswith("---")
    )
    # Build pseudo hunks for preview
    hunks: list[list[DiffLine]] = []
    hunk: list[DiffLine] = []
    old_num = 1
    new_num = 1
    for line in diff_text.splitlines():
        if line.startswith("@@"):
            if hunk:
                hunks.append(hunk)
                hunk = []
            # parse @@ -a,b +c,d @@
            try:
                parts = line.split()
                old_part = parts[1]  # -a,b
                new_part = parts[2]  # +c,d
                old_num = int(old_part[1:].split(",")[0])
                new_num = int(new_part[1:].split(",")[0])
            except Exception:
                pass
            continue
        if line.startswith("--- ") or line.startswith("+++ "):
            continue
        if line.startswith("+"):
            hunk.append(DiffLine(DiffLineKind.ADD, 0, new_num, line[1:]))
            new_num += 1
        elif line.startswith("-"):
            hunk.append(DiffLine(DiffLineKind.DELETE, old_num, 0, line[1:]))
            old_num += 1
        elif line.startswith(" ") or line.startswith("\\"):
            hunk.append(
                DiffLine(
                    DiffLineKind.CONTEXT,
                    old_num,
                    new_num,
                    line[1:] if line.startswith(" ") else line,
                )
            )
            old_num += 1
            new_num += 1
    if hunk:
        hunks.append(hunk)
    return hunks, added, removed, path


# ---------------------------------------------------------------------------
# Public: full diff panel
# ---------------------------------------------------------------------------


def render_diff_panel(
    path: str,
    hunks: list[list[DiffLine]],
    added: int,
    removed: int,
) -> RenderableType:
    title = Text()
    title.append(" ")
    title.append_text(_build_diff_header(path, added, removed))
    title.append(" ")
    highlighter = _make_highlighter(path)
    for hunk in hunks:
        _highlight_hunk(highlighter, hunk)
    max_ln = 0
    for hunk in hunks:
        for dl in hunk:
            max_ln = max(max_ln, dl.old_num, dl.new_num)
    num_width = max(len(str(max_ln)), 2)
    table = Table(show_header=False, box=None, padding=(0, 0), show_edge=False, expand=True)
    table.add_column(justify="right", width=num_width, no_wrap=True)
    table.add_column(width=3, no_wrap=True)
    table.add_column(ratio=1)
    try:
        from coderai.cli.theme import get_diff_colors

        colors = get_diff_colors()
    except Exception:
        from rich.style import Style

        colors = type("C", (), {"add_bg": Style(), "del_bg": Style()})()  # type: ignore[assignment]

    for hunk_idx, hunk in enumerate(hunks):
        if hunk_idx > 0:
            table.add_row(Text("⋮", style="dim"), Text(""), Text(""))
        for dl in hunk:
            assert dl.content is not None
            if dl.kind == DiffLineKind.ADD:
                table.add_row(
                    Text(str(dl.new_num)),
                    Text(" + ", style="green"),
                    dl.content,
                    style=colors.add_bg,
                )
            elif dl.kind == DiffLineKind.DELETE:
                table.add_row(
                    Text(str(dl.old_num)), Text(" - ", style="red"), dl.content, style=colors.del_bg
                )
            else:
                table.add_row(Text(str(dl.new_num), style="dim"), Text("   "), dl.content)
    return Panel(table, title=title, title_align="left", border_style="dim", padding=(0, 1))


def render_diff_preview_structured(
    path: str,
    hunks: list[list[DiffLine]],
    added: int,
    removed: int,
    max_lines: int = MAX_PREVIEW_CHANGED_LINES,
) -> tuple[list[RenderableType], int]:
    highlighter = _make_highlighter(path)
    for hunk in hunks:
        _highlight_hunk(highlighter, hunk)
    changed: list[DiffLine] = []
    for hunk in hunks:
        for dl in hunk:
            if dl.kind != DiffLineKind.CONTEXT:
                changed.append(dl)
    total = len(changed)
    shown = changed[:max_lines]
    remaining = total - len(shown)
    max_ln = max(
        (dl.old_num if dl.kind == DiffLineKind.DELETE else dl.new_num for dl in shown), default=0
    )
    num_width = max(len(str(max_ln)), 2)
    result: list[RenderableType] = [_build_diff_header(path, added, removed)]
    for dl in shown:
        assert dl.content is not None
        line = Text()
        ln = dl.old_num if dl.kind == DiffLineKind.DELETE else dl.new_num
        line.append(str(ln).rjust(num_width), style="dim")
        marker_style = "green" if dl.kind == DiffLineKind.ADD else "red"
        marker_char = "+" if dl.kind == DiffLineKind.ADD else "-"
        line.append(f" {marker_char} ", style=marker_style)
        line.append_text(dl.content)
        result.append(line)
    if remaining > 0:
        result.append(
            Text(f"... {remaining} more lines (press Enter to expand)", style="dim italic")
        )
    return result, remaining


def render_diff_summary_panel(path: str, description: str) -> RenderableType:
    title = Text()
    title.append(" ")
    title.append(path)
    title.append(" ")
    body = Text()
    body.append("File too large for inline diff", style="dim italic")
    body.append("\n")
    body.append(description, style="dim")
    return Panel(body, title=title, title_align="left", border_style="dim", padding=(1, 2))


# ---------------------------------------------------------------------------
# Legacy render_diff_preview (string path) — now enhanced with Panel
# ---------------------------------------------------------------------------


MAX_DIFF_LINES = 500  # ponytail: guard large diffs, show summary if exceeded


def render_diff_preview(console: Any | None, diff_text: str, title: str = "Diff Preview") -> None:
    """Render a clean, compact diff preview. Enhanced: uses structured panel when possible."""
    if not diff_text.strip():
        return
    # Truncation safeguard for large files
    lines = diff_text.splitlines()
    if len(lines) > MAX_DIFF_LINES:
        diff_text = "\n".join(lines[:MAX_DIFF_LINES])
        diff_text += (
            f"\n... truncated {len(lines) - MAX_DIFF_LINES} lines (file too large for inline diff)"
        )
    # ANSI leak guard: ensure we don't emit raw escapes from file content via format_diff_text
    # (format_diff_text already styles, but caller may have raw ANSI in diff lines — strip at render)
    try:
        from coderai.cli.statusline import strip_ansi

        diff_text = strip_ansi(diff_text)
    except Exception:
        pass
    active_console = console or Console()
    # Try structured rendering
    try:
        hunks, added, removed, path = parse_unified_diff_to_hunks(diff_text)
        if hunks:
            # Use preview (changed-only) with fallback to full panel for small diffs
            preview_lines, remaining = render_diff_preview_structured(path, hunks, added, removed)
            header = Text()
            header.append("    ↳ ", style="dim cyan")
            header.append(title, style="bold cyan")
            if added > 0 or removed > 0:
                header.append(f" +{added}", style="bold green")
                header.append(f" -{removed}", style="bold red")
            active_console.print(header)
            for item in preview_lines:
                # indent
                if isinstance(item, Text):
                    # Wrap in indented text
                    indented = Text("      ")
                    indented.append_text(item)
                    active_console.print(indented)
                else:
                    active_console.print(item)
            if remaining == 0 and len(hunks) == 1 and len(hunks[0]) <= 20:
                # Also show full panel for tiny diffs
                pass
            return
    except Exception:
        pass

    # Fallback: legacy flat rendering
    added, removed = parse_diff_stats(diff_text)
    header = Text()
    header.append("    ↳ ", style="dim cyan")
    header.append(title, style="bold cyan")
    if added > 0 or removed > 0:
        header.append(f" +{added}", style="bold green")
        header.append(f" -{removed}", style="bold red")
    active_console.print(header)
    diff_body = Text()
    for line in diff_text.splitlines():
        if line.startswith("--- ") or line.startswith("+++ "):
            diff_body.append(f"      {line}\n", style="bold cyan")
        elif line.startswith("@@"):
            diff_body.append(f"      {line}\n", style="bold magenta")
        elif line.startswith("+"):
            diff_body.append(f"      {line}\n", style="green")
        elif line.startswith("-"):
            diff_body.append(f"      {line}\n", style="red")
        elif line.startswith("\\"):
            diff_body.append(f"      {line}\n", style="dim italic")
        else:
            diff_body.append(f"      {line}\n", style="dim")
    active_console.print(diff_body)
