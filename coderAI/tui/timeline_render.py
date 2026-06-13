"""Timeline row renderers for the Textual chat UI."""

from __future__ import annotations

import logging
import time as _time_mod
from typing import Any, Dict, Protocol

from rich.markup import escape
from rich.text import Text
from rich.markdown import Markdown
from rich.padding import Padding

from coderAI.tui.diff_render import format_diff_gutter
from coderAI.tui.theme import Glyphs, Styles, Tokens

logger = logging.getLogger(__name__)


class SupportsWrite(Protocol):
    """Minimal write sink for timeline rendering.

    Both Textual's ``RichLog`` widget and the ``RecordingLog`` capture buffer
    used for render caching satisfy this structurally — the renderers only ever
    call ``write``.
    """

    def write(self, renderable: Any) -> Any: ...


_TOAST_STYLES = {
    "info": Tokens.INFO,
    "success": Tokens.AGENT,
    "warning": Tokens.WARN,
    "error": Tokens.DANGER,
}


def _fmt_ts(ts: float | None) -> str:
    if ts is None:
        return ""
    lt = _time_mod.localtime(ts)
    return f"[{Tokens.TEXT_MUTED}]{lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d}[/]"


def _first_lines(text: str, n: int) -> str:
    lines = text.splitlines()
    if len(lines) <= n:
        return text
    return "\n".join(lines[:n]) + f"\n[… {len(lines) - n} more lines]"


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
    else:
        logger.warning("Unknown timeline kind: %s", kind)


def build_stream_tail_markup(it: Dict[str, Any], *, verbose: bool) -> str:
    """Rich markup for the live streaming assistant tail."""
    ts = _fmt_ts(it.get("ts"))
    lines: list[str] = []
    reasoning = (it.get("reasoning") or "").strip()
    if verbose and reasoning:
        lines.append(
            f"[{Styles.REASONING_GLYPH}]{Glyphs.REASONING}[/] "
            f"[{Styles.REASONING_LABEL}]reasoning[/]" + (f"  {ts}" if ts else "")
        )
        lines.append(f"  [{Styles.REASONING}]{escape(reasoning)}[/]")
        lines.append("")
    lines.append(
        f"[{Styles.ASSISTANT_GLYPH}]{Glyphs.ASSISTANT}[/] [{Styles.ASSISTANT}]assistant[/]"
        + (f"  {ts}" if ts else "")
    )
    content = it.get("content", "") or ""
    if content:
        lines.append(f"  [{Styles.TEXT}]{escape(content)}[/]")
    lines.append(f"  [{Tokens.AGENT}]▌[/]")
    return "\n".join(lines)


def write_user(log: SupportsWrite, it: Dict[str, Any]) -> None:
    ts = _fmt_ts(it.get("ts"))
    header = Text()
    header.append(f"{Glyphs.USER} ", style=Styles.USER_GLYPH)
    header.append("you", style=Styles.USER)
    if ts:
        header.append(f"  {ts}")
    log.write(header)
    body = it.get("text", "") or ""
    if body:
        if it.get("collapsed"):
            log.write(Text.from_markup(f"  [{Tokens.TEXT_DIM}]{escape(_first_lines(body, 2))}[/]"))
        else:
            log.write(Padding(Markdown(body), (0, 0, 0, 2)))
    log.write("")


def write_assistant(log: SupportsWrite, it: Dict[str, Any], verbose: bool) -> None:
    ts = _fmt_ts(it.get("ts"))
    collapsed = it.get("collapsed")
    reasoning = (it.get("reasoning") or "").strip()
    if verbose and reasoning and not collapsed:
        head = Text()
        head.append(f"{Glyphs.REASONING} ", style=Styles.REASONING_GLYPH)
        head.append("reasoning", style=Styles.REASONING_LABEL)
        if ts:
            head.append(f"  {ts}")
        log.write(head)
        log.write(Text("  " + reasoning, style=Styles.REASONING))
        log.write("")
    head = Text()
    head.append(f"{Glyphs.ASSISTANT} ", style=Styles.ASSISTANT_GLYPH)
    head.append("assistant", style=Styles.ASSISTANT)
    if ts:
        head.append(f"  {ts}")
    log.write(head)
    content = it.get("content", "")
    if content:
        if collapsed:
            log.write(
                Text.from_markup(f"  [{Tokens.TEXT_DIM}]{escape(_first_lines(content, 3))}[/]")
            )
        else:
            log.write(Padding(Markdown(content), (0, 0, 0, 2)))
    if it.get("streaming") and not collapsed:
        log.write(Text("  ▌", style=f"blink {Tokens.AGENT}"))
    log.write("")


def write_tool(log: SupportsWrite, it: Dict[str, Any]) -> None:
    ts = _fmt_ts(it.get("ts"))
    collapsed = it.get("collapsed")
    ok = it.get("ok")
    if ok is True:
        glyph, glyph_color, border_color = Glyphs.TOOL_OK, Tokens.AGENT, Tokens.AGENT
    elif ok is False:
        glyph, glyph_color, border_color = Glyphs.ERROR, Tokens.DANGER, Tokens.DANGER
    else:
        glyph, glyph_color, border_color = Glyphs.TOOL_RUN, Tokens.THOUGHT, Tokens.THOUGHT

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
        row.append(f" {ts}")
    if args_str and not collapsed:
        row.append(f" {args_str}", style=Styles.TOOL_ARGS)
    if preview and not collapsed:
        row.append(f"   {preview}", style=Styles.TOOL_PREVIEW)
    if collapsed and (args_str or preview):
        row.append(f" [{Tokens.TEXT_MUTED}][…][/]")
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
        head.append(f"  {ts}")
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
        head.append(f"  {ts}")
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
        if status == "done":
            g, c = Glyphs.TOOL_OK, Tokens.AGENT
        elif idx == current + 1:
            g, c = "▸", Tokens.WARN
        else:
            g, c = "·", Tokens.TEXT_MUTED
        markup = f"[{c}]{g}[/] [{Tokens.TEXT_MUTED}]{idx}.[/] [{Tokens.TEXT}]{desc}[/]"
        log.write(Text.from_markup(f"  {markup}"))
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
    return 3
