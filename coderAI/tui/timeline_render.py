"""Timeline row renderers and length management for the Textual chat UI."""

from __future__ import annotations

import logging
import time as _time_mod
from typing import Any, Callable, Dict, List, Protocol

from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.markup import escape
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from rich.markdown import Markdown
from rich.padding import Padding

from coderAI.tui.diff_render import format_diff_gutter
from coderAI.tui.platform import palette_shortcut
from coderAI.tui.theme import Categories, Glyphs, Styles, Tokens

logger = logging.getLogger(__name__)

MAX_TIMELINE = 500
KEEP_AFTER_TRIM = 400


def append_capped(
    timeline: List[Dict[str, Any]],
    item: Dict[str, Any],
    next_id: Callable[[], str],
) -> List[Dict[str, Any]]:
    out = timeline + [item]
    if len(out) <= MAX_TIMELINE:
        return out
    trimmed = out[-KEEP_AFTER_TRIM:]
    sep = {
        "kind": "separator",
        "id": next_id(),
        "message": f"History trimmed ({len(out) - KEEP_AFTER_TRIM} older messages hidden)",
    }
    return [sep] + trimmed


class SupportsWrite(Protocol):
    """Minimal write sink for timeline rendering.

    Textual's ``RichLog`` widget satisfies this structurally (the renderers only
    ever call ``write``), as do the lightweight recording buffers used in tests.
    """

    def write(self, renderable: Any) -> Any: ...


_TOAST_STYLES = {
    "info": Tokens.INFO,
    "success": Tokens.AGENT,
    "warning": Tokens.WARN,
    "error": Tokens.DANGER,
}


def plan_step_marker(status: str, idx: int, current: int) -> tuple[str, str]:
    """Return the ``(glyph, color)`` marker for a plan step row."""
    if status == "done":
        return Glyphs.TOOL_OK, Tokens.AGENT
    if idx == current + 1:
        return "▸", Tokens.WARN
    return "·", Tokens.TEXT_MUTED


def _fmt_ts(ts: float | None) -> str:
    """Plain ``HH:MM:SS`` — style/markup is applied at the call site
    (``Text.append`` does not parse markup, so returning markup here would
    render the tags literally)."""
    if ts is None:
        return ""
    lt = _time_mod.localtime(ts)
    return f"{lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d}"


def _first_lines(text: str, n: int) -> str:
    lines = text.splitlines()
    if len(lines) <= n:
        return text
    return "\n".join(lines[:n]) + f"\n[… {len(lines) - n} more lines]"


class _RailBlock:
    """Render ``renderable`` with a colored ``▎`` left rail on every visual line.

    The Rail is the design system's backbone motif: a single colored left-edge
    pipe that groups a block with minimal visual weight (see the Tokyo Night
    "Rail" redesign). Tool cards/diffs draw a single-line rail inline; this
    wrapper extends the same gutter down a multi-line block (chat bubbles).
    """

    def __init__(self, renderable: Any, color: str) -> None:
        self.renderable = renderable
        self.color = color

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        gutter = Segment("▎ ", Style(color=self.color))
        inner = options.update_width(max(1, options.max_width - 2))
        for line in console.render_lines(self.renderable, inner, pad=False):
            yield gutter
            yield from line
            yield Segment.line()


def write_timeline_item(log: SupportsWrite, it: Dict[str, Any], *, verbose: bool) -> None:
    kind = it.get("kind")
    if kind == "user":
        write_user(log, it)
    elif kind == "assistant":
        write_assistant(log, it, verbose)
    elif kind == "tool":
        write_tool(log, it)
    elif kind == "diff":
        write_diff(log, it, verbose)
    elif kind == "error":
        write_error(log, it)
    elif kind == "toast":
        write_toast(log, it)
    elif kind == "separator":
        log.write(Text(f"— {it.get('message')} —", style=Styles.TEXT_DIM))
    elif kind == "approval":
        write_approval(log, it)
    elif kind == "skill_card":
        write_skill_card(log, it)
    elif kind == "plan_card":
        write_plan_card(log, it)
    elif kind == "welcome":
        write_welcome(log, it)
    else:
        logger.warning("Unknown timeline kind: %s", kind)


# The live tail widget is height-capped (max-height: 40%), so only the end of
# the accumulated message is ever visible. Re-escaping the full text every
# flush is O(n²) over a long response — render just the tail instead; the
# complete message goes through the normal timeline path once the turn ends.
_STREAM_TAIL_CHARS = 4000


def _tail_slice(text: str, limit: int = _STREAM_TAIL_CHARS) -> str:
    if len(text) <= limit:
        return text
    cut = text[-limit:]
    nl = cut.find("\n")
    return cut[nl + 1 :] if nl != -1 else cut


def build_stream_tail_markup(it: Dict[str, Any], *, verbose: bool) -> str:
    """Rich markup for the live streaming assistant tail."""
    ts = _fmt_ts(it.get("ts"))
    lines: list[str] = []
    reasoning = (it.get("reasoning") or "").strip()
    if verbose and reasoning:
        lines.append(
            f"[{Styles.REASONING_GLYPH}]{Glyphs.REASONING}[/] "
            f"[{Styles.REASONING_LABEL}]reasoning[/]"
            + (f"  [{Tokens.TEXT_MUTED}]{ts}[/]" if ts else "")
        )
        lines.append(f"  [{Styles.REASONING}]{escape(_tail_slice(reasoning))}[/]")
        lines.append("")
    lines.append(
        f"[{Styles.ASSISTANT_GLYPH}]{Glyphs.ASSISTANT}[/] [{Styles.ASSISTANT}]assistant[/]"
        + (f"  [{Tokens.TEXT_MUTED}]{ts}[/]" if ts else "")
    )
    content = it.get("content", "") or ""
    if content:
        lines.append(f"  [{Styles.TEXT}]{escape(_tail_slice(content))}[/]")
    lines.append(f"  [{Tokens.AGENT}]▌[/]")
    return "\n".join(lines)


def write_user(log: SupportsWrite, it: Dict[str, Any]) -> None:
    ts = _fmt_ts(it.get("ts"))
    header = Text()
    header.append(f"{Glyphs.USER} ", style=Styles.USER_GLYPH)
    header.append("you", style=Styles.USER)
    if ts:
        header.append(f"  {ts}", style=Tokens.TEXT_MUTED)
    parts: list[Any] = [header]
    body = it.get("text", "") or ""
    if body:
        if it.get("collapsed"):
            parts.append(
                Text.from_markup(f"  [{Tokens.TEXT_DIM}]{escape(_first_lines(body, 2))}[/]")
            )
        else:
            parts.append(Padding(Markdown(body), (0, 0, 0, 2)))
    log.write(_RailBlock(Group(*parts), Tokens.INFO))
    log.write("")


def write_assistant(log: SupportsWrite, it: Dict[str, Any], verbose: bool) -> None:
    ts = _fmt_ts(it.get("ts"))
    collapsed = it.get("collapsed")
    reasoning = (it.get("reasoning") or "").strip()
    parts: list[Any] = []
    if verbose and reasoning and not collapsed:
        rhead = Text()
        rhead.append(f"{Glyphs.REASONING} ", style=Styles.REASONING_GLYPH)
        rhead.append("reasoning", style=Styles.REASONING_LABEL)
        if ts:
            rhead.append(f"  {ts}", style=Tokens.TEXT_MUTED)
        parts.append(rhead)
        parts.append(Text("  " + reasoning, style=Styles.REASONING))
        parts.append(Text(""))
    head = Text()
    head.append(f"{Glyphs.ASSISTANT} ", style=Styles.ASSISTANT_GLYPH)
    head.append("assistant", style=Styles.ASSISTANT)
    if ts:
        head.append(f"  {ts}", style=Tokens.TEXT_MUTED)
    parts.append(head)
    content = it.get("content", "")
    if content:
        if collapsed:
            parts.append(
                Text.from_markup(f"  [{Tokens.TEXT_DIM}]{escape(_first_lines(content, 3))}[/]")
            )
        else:
            parts.append(Padding(Markdown(content), (0, 0, 0, 2)))
    if it.get("streaming") and not collapsed:
        parts.append(Text("  ▌", style=f"blink {Tokens.AGENT}"))
    log.write(_RailBlock(Group(*parts), Tokens.AGENT))
    log.write("")


def write_tool(log: SupportsWrite, it: Dict[str, Any]) -> None:
    ts = _fmt_ts(it.get("ts"))
    collapsed = it.get("collapsed")
    ok = it.get("ok")
    # The Rail color encodes the tool category; a semantic state (error)
    # overrides it. The glyph color stays semantic (ok / running / error).
    cat_color = Categories.color(it.get("category"))
    if ok is True:
        glyph, glyph_color, border_color = Glyphs.TOOL_OK, Tokens.AGENT, cat_color
    elif ok is False:
        glyph, glyph_color, border_color = Glyphs.ERROR, Tokens.DANGER, Tokens.DANGER
    else:
        glyph, glyph_color, border_color = Glyphs.TOOL_RUN, Tokens.THOUGHT, cat_color

    name = str(it.get("name") or "")
    args = it.get("args") or {}
    if isinstance(args, dict):
        argbits = []
        for k in (
            "path",
            "file_path",
            "command",
            "query",
            "url",
            "pattern",
            "target",
            "content",
            "regex",
            "text",
            "code",
            "message",
            "description",
            "search",
        ):
            if k in args:
                argbits.append(str(args[k]))
                break
        args_str = argbits[0] if argbits else ""
    else:
        args_str = str(args)
    args_str = args_str.replace("\n", " ")[:60]

    preview = str(it.get("preview") or "")[:60]

    row = Text()
    row.append("▎", style=border_color)
    row.append(" ")
    row.append(f"{glyph} ", style=glyph_color)
    row.append(f"{name:<16}", style=Styles.TOOL_NAME)
    if ts:
        row.append(f" {ts}", style=Tokens.TEXT_MUTED)
    if args_str and not collapsed:
        row.append(f" {args_str}", style=Styles.TOOL_ARGS)
    if preview and not collapsed:
        row.append(f"   {preview}", style=Styles.TOOL_PREVIEW)
    if collapsed and (args_str or preview):
        row.append(f" [{Tokens.TEXT_MUTED}][…][/]")
    # Risk is earned: low risk gets no badge, medium/high are flagged inline.
    risk = str(it.get("risk") or "low")
    if risk in ("medium", "high") and not collapsed:
        row.append(
            f"  {Glyphs.APPROVAL} {'high' if risk == 'high' else 'med'}",
            style=Tokens.DANGER if risk == "high" else Tokens.WARN,
        )
    log.write(row)
    if it.get("error") and not collapsed:
        log.write(Text(f"    → {it['error']}", style=f"{Tokens.DANGER}"))


def write_diff(log: SupportsWrite, it: Dict[str, Any], verbose: bool) -> None:
    ts = _fmt_ts(it.get("ts"))
    collapsed = it.get("collapsed")
    path = it.get("path", "")
    head = Text()
    head.append("▎ ", style=Tokens.LINE_SOFT)
    head.append(f"{Glyphs.TOOL_OK} ", style=Styles.TOOL_OK)
    head.append("diff", style=Styles.TOOL_NAME)
    head.append(f"  {path}", style=Tokens.TEXT_DIM)
    if ts:
        head.append(f"  {ts}", style=Tokens.TEXT_MUTED)
    log.write(head)
    if collapsed:
        diff_body = it.get("diff", "")
        line_count = diff_body.count("\n") + (1 if diff_body else 0)
        log.write(Text.from_markup(f"  [{Tokens.TEXT_MUTED}]{line_count} lines[/]"))
    else:
        body = it.get("diff", "")
        max_lines = 40 if verbose else 12
        rendered = format_diff_gutter(body, max_lines=max_lines)
        log.write(Text.from_markup(rendered))


def write_error(log: SupportsWrite, it: Dict[str, Any]) -> None:
    ts = _fmt_ts(it.get("ts"))
    head = Text()
    head.append(f"{Glyphs.ERROR} ", style=Tokens.DANGER)
    head.append("error", style=Styles.DANGER)
    if ts:
        head.append(f"  {ts}", style=Tokens.TEXT_MUTED)
    log.write(head)
    log.write(Text("  " + str(it.get("message", "")), style=Styles.TEXT))
    if it.get("hint"):
        log.write(Text("  " + str(it["hint"]), style=Styles.TEXT_DIM))


def write_toast(log: SupportsWrite, it: Dict[str, Any]) -> None:
    level = it.get("level", "info")
    color = _TOAST_STYLES.get(level, Tokens.TEXT_DIM)
    log.write(Text("· " + str(it.get("message", "")), style=color))


def write_approval(log: SupportsWrite, it: Dict[str, Any]) -> None:
    decided = it.get("decided", "pending")
    head = Text()
    head.append(f"{Glyphs.APPROVAL} ", style=Styles.APPROVAL_GLYPH)
    head.append("approval required", style=Styles.APPROVAL_LABEL)
    head.append(f"  {it.get('tool', '')}", style=Styles.TEXT)
    head.append(f"  [{decided}]", style=Styles.TEXT_MUTED)
    log.write(head)


def write_skill_card(log: SupportsWrite, it: Dict[str, Any]) -> None:
    name = escape(str(it.get("name") or ""))
    desc = escape(str(it.get("description") or ""))
    steps = it.get("steps") or []
    total = len(steps)

    head = Text.from_markup(
        f"[{Tokens.LINE_SOFT}]▎[/] [{Tokens.TEXT_DIM}]SKILL[/] · [{Tokens.TEXT}]{name}[/]"
        + (f" · [{Tokens.TEXT_MUTED}]{total} steps[/]" if total else "")
    )
    log.write(head)
    if desc:
        log.write(Text.from_markup(f"  [{Tokens.TEXT_DIM}]{desc[:120]}[/]"))

    if steps:
        for s in steps[:12]:
            idx = int(s.get("index", 0))
            label = escape(str(s.get("label", ""))[:100])
            log.write(
                Text.from_markup(f"  [{Tokens.TEXT_MUTED}]{idx:>2}.[/] [{Tokens.TEXT}]{label}[/]")
            )
    log.write("")


def write_plan_card(log: SupportsWrite, it: Dict[str, Any]) -> None:
    title = str(it.get("title") or "")
    completed = int(it.get("completed") or 0)
    total = int(it.get("total") or 0)
    current = int(it.get("currentIdx") or 0)
    steps = it.get("steps") or []

    head = Text()
    head.append("▎ ", style=Tokens.LINE_SOFT)
    head.append("PLAN", style=Tokens.TEXT_DIM)
    head.append(f" · {title}", style=Tokens.TEXT)
    if total:
        head.append(f" · {completed}/{total}", style=Tokens.TEXT_MUTED)
    log.write(head)

    for s in steps[:12]:
        idx = int(s.get("index", 0))
        status = str(s.get("status", "pending"))
        desc = str(s.get("description", ""))[:100]
        g, c = plan_step_marker(status, idx, current)
        markup = f"[{c}]{g}[/] [{Tokens.TEXT_MUTED}]{idx}.[/] [{Tokens.TEXT}]{desc}[/]"
        log.write(Text.from_markup(f"  {markup}"))
    log.write("")


def write_welcome(log: SupportsWrite, it: Dict[str, Any]) -> None:
    """Empty-state block seeded at session start.

    Renders exactly 7 lines (6 rail lines + trailing blank) — keep
    ``calculate_item_lines`` in sync when adding or removing a line.
    """
    model = escape(str(it.get("model") or "…"))
    provider = escape(str(it.get("provider") or ""))
    cwd = str(it.get("cwd") or "")
    if len(cwd) > 60:
        cwd = "…" + cwd[-59:]
    head = Text()
    head.append(f"{Glyphs.BRAND} ", style=f"bold {Tokens.ACCENT}")
    head.append("CoderAI", style=f"bold {Tokens.TEXT}")
    session_line = model + (f" · {provider}" if provider else "")
    pal = palette_shortcut()
    parts: list[Any] = [
        head,
        Text.from_markup(f"  [{Tokens.TEXT_DIM}]{session_line}[/]"),
        Text.from_markup(f"  [{Tokens.TEXT_MUTED}]{escape(cwd)}[/]"),
        Text(""),
        Text.from_markup(
            f"  [{Tokens.TEXT_MUTED}]↵ send · @ mention files · / commands · {pal} palette[/]"
        ),
        Text.from_markup(f"  [{Tokens.TEXT_MUTED}]PgUp/PgDn scrollback · ^B agents · ^G plan[/]"),
    ]
    log.write(_RailBlock(Group(*parts), Tokens.ACCENT))
    log.write("")


def calculate_item_lines(it: Dict[str, Any], verbose: bool) -> int:
    """Precisely calculate the height (in lines) of a rendered timeline item."""
    kind = it.get("kind")
    if kind == "user":
        body = it.get("text", "") or ""
        if it.get("collapsed"):
            parsed_len = len(body.splitlines())
            body_lines = parsed_len if parsed_len <= 2 else 3
        else:
            body_lines = len(body.splitlines())
        return 1 + body_lines + 1
    elif kind == "assistant":
        collapsed = it.get("collapsed")
        lines = 0
        reasoning = (it.get("reasoning") or "").strip()
        if verbose and reasoning and not collapsed:
            lines += 1  # reasoning header
            lines += len(reasoning.splitlines())  # reasoning body
            lines += 1  # empty line
        lines += 1  # assistant header
        content = it.get("content", "")
        if content:
            if collapsed:
                parsed_len = len(content.splitlines())
                content_lines = parsed_len if parsed_len <= 3 else 4
            else:
                content_lines = len(content.splitlines())
            lines += content_lines
        if it.get("streaming") and not collapsed:
            lines += 1  # cursor block
        lines += 1  # empty line
        return lines
    elif kind == "tool":
        collapsed = it.get("collapsed")
        lines = 1
        if it.get("error") and not collapsed:
            lines += 1
        return lines
    elif kind == "diff":
        collapsed = it.get("collapsed")
        if collapsed:
            return 2  # header + "N lines" text
        else:
            body = it.get("diff", "")
            parsed_len = len(body.splitlines())
            max_lines = 40 if verbose else 12
            if parsed_len <= max_lines:
                diff_lines = parsed_len
            else:
                diff_lines = (max_lines // 2) * 2 + 1
            return 1 + diff_lines
    elif kind == "error":
        lines = 1  # header
        lines += len(str(it.get("message", "")).splitlines())
        if it.get("hint"):
            lines += len(str(it["hint"]).splitlines())
        return lines
    elif kind in ("toast", "separator", "approval"):
        return 1
    elif kind == "skill_card":
        lines = 1
        desc = str(it.get("description") or "")
        if desc:
            lines += 1
        steps = it.get("steps") or []
        lines += min(len(steps), 12)
        lines += 1  # empty line
        return lines
    elif kind == "plan_card":
        lines = 1
        steps = it.get("steps") or []
        lines += min(len(steps), 12)
        lines += 1  # empty line
        return lines
    elif kind == "welcome":
        return 7  # 6 rail lines + trailing blank (see write_welcome)
    return 3
