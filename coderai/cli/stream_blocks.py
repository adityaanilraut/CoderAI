"""Live visualizer blocks for streaming and progress output.

Single Live(Group, transient, vertical_overflow=visible) handles streaming
markdown commitment, thinking pulses, and status/notification blocks.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from rich.console import Group, RenderableType
from rich.spinner import Spinner
from rich.style import Style
from rich.text import Text

# reuse CoderAI console (MANPAGER-safe, neutral theme)
try:
    from coderai.cli.console import console  # type: ignore[assignment]
except Exception:  # fallback for tests
    from rich.console import Console

    console = Console()  # type: ignore[no-redef]

from coderai.cli.columns import BulletColumns
from coderai.cli.elapsed import (
    _estimate_tokens_float as _estimate_tokens,
    bullet_frame_for,
    format_context_status,
    format_elapsed,
    format_token_count_compact as format_token_count,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_ELLIPSIS = "..."
_THINKING_PREVIEW_LINES = 6
_SELF_CLOSING_BLOCKS = frozenset(("fence", "code_block", "hr", "html_block"))
MAX_LIVE_NOTIFICATIONS = 4
MAX_SUBAGENT_TOOL_CALLS_TO_SHOW = 4

_BULLET_FRAMES = (".  ", ".. ", "...", " ..", "  .", "   ")
_BULLET_FRAME_INTERVAL = 0.13

# Lazy markdown-it parser
_md_parser: Any | None = None


def _get_md_parser() -> Any | None:
    global _md_parser
    if _md_parser is not None:
        return _md_parser
    try:
        from markdown_it import MarkdownIt

        _md_parser = MarkdownIt().enable("strikethrough").enable("table")
        return _md_parser
    except Exception:
        return None


# canonical bullet helper — re-export for backward compat
_bullet_frame_for = bullet_frame_for


def _truncate_to_display_width(line: str, max_width: int) -> str:
    from rich.cells import cell_len

    if cell_len(line) <= max_width:
        return line
    ellipsis_width = cell_len(_ELLIPSIS)
    budget = max_width - ellipsis_width
    width = 0
    for i, ch in enumerate(line):
        width += cell_len(ch)
        if width > budget:
            return line[:i] + _ELLIPSIS
    return line


def _tail_lines(text: str, n: int) -> str:
    """Last n lines via reverse scan — O(n)."""
    pos = len(text)
    for _ in range(n):
        pos = text.rfind("\n", 0, pos)
        if pos == -1:
            return text
    return text[pos + 1 :]


def _find_committed_boundary(text: str) -> int | None:
    """Parser-aware boundary — Kimi _blocks.py:128 parity. None if <2 blocks."""
    # Prefer delegating to markdown_stream helper if available (same logic)
    # but markdown_stream returns 0 instead of None for authorative fallback —
    # so we re-implement here faithfully.
    md = _get_md_parser()
    if md is None:
        return None
    try:
        tokens = md.parse(text)
    except Exception:
        return None
    block_maps: list[list[int]] = []
    depth = 0
    for t in tokens:
        if t.nesting == 1:
            if depth == 0 and t.map is not None:
                block_maps.append(t.map)
            depth += 1
        elif t.nesting == -1:
            depth -= 1
        elif depth == 0 and t.type in _SELF_CLOSING_BLOCKS and t.map is not None:
            block_maps.append(t.map)
    if len(block_maps) < 2:
        return None
    target_line = block_maps[-2][1]
    offset = 0
    # ponytail: O(n) scan, matches Kimi 161
    try:
        for _ in range(target_line):
            offset = text.index("\n", offset) + 1
    except ValueError:
        return None
    return offset


# Markdown import — prefer Kimi-style wrapper if exists, else rich
try:
    from coderai.cli.syntax_theme import KIMI_ANSI_THEME  # noqa: F401

    from rich.markdown import Markdown  # type: ignore[assignment]
except Exception:
    try:
        from rich.markdown import Markdown  # type: ignore[no-redef]
    except Exception:
        Markdown = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# _ContentBlock — core streaming block Kimi 176-361
# ---------------------------------------------------------------------------
class _ContentBlock:
    """Streaming content block with incremental markdown commitment.

    For **composing** (is_think=False), confirmed markdown blocks are flushed
    to terminal via console.print() as they become complete; only tail stays
    in transient Live area.

    For **thinking** (is_think=True), raw reasoning is hidden by default;
    Live shows italic Thinking + bullet + tok/s pulse, final is grey italic
    one-liner. With show_thinking_stream=True, legacy preview mode shows
    spinner + 6-line tail.
    """

    def __init__(self, is_think: bool, *, show_thinking_stream: bool = False):
        self.is_think = is_think
        self._show_thinking_stream = show_thinking_stream
        self._spinner = Spinner("dots", "")
        self.raw_text = ""
        self._token_count: float = 0.0
        self._start_time = time.monotonic()
        self._committed_len = 0
        self._has_printed_bullet = False

    # -- Public API ----------------------------------------------------------
    def append(self, content: str) -> None:
        self.raw_text += content
        self._token_count += _estimate_tokens(content)
        if not self.is_think and "\n" in content:
            self._flush_committed()

    def compose(self) -> RenderableType:
        if self.is_think:
            if self._show_thinking_stream:
                return self._compose_thinking_stream()
            return self._compose_thinking()
        return self._compose_spinner()

    def compose_final(self) -> RenderableType:
        if self.is_think:
            if self._show_thinking_stream:
                remaining = self._pending_text()
                if not remaining:
                    return Text("")
                if Markdown is not None:
                    try:
                        return BulletColumns(
                            Markdown(remaining, style="grey50 italic"),
                            bullet_style="grey50",
                        )
                    except Exception:
                        pass
                return Text(remaining, style="grey50 italic")
            elapsed_str = format_elapsed(time.monotonic() - self._start_time)
            count_str = format_token_count(int(self._token_count))
            return Text(f"Thought for {elapsed_str} · {count_str} tokens", style="grey50 italic")
        remaining = self._pending_text()
        if not remaining:
            return Text("")
        if Markdown is not None:
            try:
                return self._wrap_bullet(Markdown(remaining))
            except Exception:
                pass
        return Text(remaining)

    def has_pending(self) -> bool:
        if self.is_think:
            return bool(self.raw_text)
        return bool(self._pending_text())

    # -- Private -------------------------------------------------------------
    def _pending_text(self) -> str:
        return self.raw_text[self._committed_len :]

    def _wrap_bullet(self, renderable: RenderableType) -> BulletColumns:
        if self._has_printed_bullet:
            return BulletColumns(renderable, bullet=Text(" "))
        self._has_printed_bullet = True
        return BulletColumns(renderable)

    def _flush_committed(self) -> None:
        pending = self._pending_text()
        if not pending:
            return
        boundary = _find_committed_boundary(pending)
        if boundary is None:
            return
        committed_text = pending[:boundary]
        try:
            if Markdown is not None:
                console.print(self._wrap_bullet(Markdown(committed_text)))
            else:
                console.print(self._wrap_bullet(Text(committed_text)))
        except Exception:
            try:
                console.print(committed_text)
            except Exception:
                pass
        self._committed_len += boundary
        remaining = self._pending_text()
        if remaining.startswith("\n"):
            try:
                console.print()
            except Exception:
                pass
            self._committed_len += 1

    def _compose_spinner(self) -> Spinner:
        elapsed = time.monotonic() - self._start_time
        elapsed_str = format_elapsed(elapsed)
        count_str = f"{format_token_count(int(self._token_count))} tokens"
        self._spinner.text = Text.assemble(
            ("Composing...", ""),
            (f" {elapsed_str}", "grey50"),
            (f" · {count_str}", "grey50"),
        )
        return self._spinner

    def _compose_thinking_stream(self) -> RenderableType:
        spinner = self._compose_thinking_spinner()
        pending = self._pending_text()
        if not pending:
            return spinner
        preview = self._build_preview(pending)
        return Group(spinner, Text(preview, style="grey50 italic"))

    def _compose_thinking_spinner(self) -> Spinner:
        elapsed = time.monotonic() - self._start_time
        elapsed_str = format_elapsed(elapsed)
        count_str = f"{format_token_count(int(self._token_count))} tokens"
        self._spinner.text = Text.assemble(
            ("Thinking...", ""),
            (f" {elapsed_str}", "grey50"),
            (f" · {count_str}", "grey50"),
        )
        return self._spinner

    def _build_preview(self, text: str) -> str:
        max_width = console.width - 2 if getattr(console, "width", 0) else 78
        tail_text = _tail_lines(text, _THINKING_PREVIEW_LINES)
        lines = tail_text.split("\n")
        return "\n".join(_truncate_to_display_width(line, max_width) for line in lines)

    def _compose_thinking(self) -> Text:
        elapsed = time.monotonic() - self._start_time
        elapsed_str = format_elapsed(elapsed)
        tokens_int = int(self._token_count)
        count_str = f"{format_token_count(tokens_int)} tokens"
        frame = _bullet_frame_for(elapsed)
        parts: list[tuple[str, str | Style]] = [
            ("Thinking", "italic"),
            (f" {frame}", "cyan"),
            (f"  {elapsed_str}", "grey50"),
            (f" · {count_str}", "grey50"),
        ]
        if elapsed > 0.5 and tokens_int > 0:
            rate = int(tokens_int / elapsed)
            if rate > 0:
                parts.append((f" · {rate} tok/s", "grey50"))
        return Text.assemble(*parts)


# Alias for external importers
ContentBlock = _ContentBlock


# ---------------------------------------------------------------------------
# Notification / Status blocks — Kimi 594-640
# ---------------------------------------------------------------------------
@dataclass
class Notification:
    title: str
    body: str = ""
    severity: str = "info"


@dataclass
class StatusUpdate:
    context_usage: float | None = None
    context_tokens: int | None = None
    max_context_tokens: int | None = None


class _NotificationBlock:
    _SEVERITY_STYLE = {
        "info": "cyan",
        "success": "green",
        "warning": "yellow",
        "error": "red",
    }

    def __init__(self, notification: Notification):
        self.notification = notification

    def compose(self) -> RenderableType:
        style = self._SEVERITY_STYLE.get(self.notification.severity, "cyan")
        lines: list[RenderableType] = [Text(self.notification.title, style=f"bold {style}")]
        body = self.notification.body.strip()
        if body:
            body_lines = body.splitlines()
            preview = "\n".join(body_lines[:2])
            if len(body_lines) > 2:
                preview += "\n..."
            lines.append(Text(preview, style="grey50"))
        return BulletColumns(Group(*lines), bullet_style=style)


class _StatusBlock:
    def __init__(self, initial: StatusUpdate | None = None) -> None:
        self.text = Text("", justify="right")
        self._context_usage: float = 0.0
        self._context_tokens: int = 0
        self._max_context_tokens: int = 0
        if initial is not None:
            self.update(initial)

    def render(self) -> RenderableType:
        return self.text

    def update(self, status: StatusUpdate) -> None:
        if status.context_usage is not None:
            self._context_usage = status.context_usage
        if status.context_tokens is not None:
            self._context_tokens = status.context_tokens
        if status.max_context_tokens is not None:
            self._max_context_tokens = status.max_context_tokens
        if status.context_usage is not None:
            self.text.plain = format_context_status(
                self._context_usage,
                self._context_tokens,
                self._max_context_tokens,
            )


class _TodoBlock:
    def __init__(self, block: Any) -> None:
        self.block = block

    def compose(self) -> RenderableType:
        from coderai.cli.plan_render import create_todo_block

        items = getattr(self.block, "items", self.block)
        title = getattr(self.block, "title", "Todo")
        width = console.width if getattr(console, "width", 0) else 80
        return create_todo_block(items, title=title, term_width=width)


# Public aliases
NotificationBlock = _NotificationBlock
StatusBlock = _StatusBlock
TodoBlock = _TodoBlock


# Helpers exposed for testing / Live view
def _format_step_retry(retry: Any) -> Text:
    """Minimal StepRetry banner — matches Kimi _live_view.py:82."""
    # retry may be dict or object with wait_s/next_attempt/max_attempts/status_code/error_type
    wait = getattr(retry, "wait_s", 0) or 0
    next_attempt = getattr(retry, "next_attempt", "?")
    max_attempts = getattr(retry, "max_attempts", "?")
    status_code = getattr(retry, "status_code", None)
    error_type = getattr(retry, "error_type", "") or ""
    if status_code == 429:
        reason = "rate limit"
    elif isinstance(status_code, int) and status_code >= 500:
        reason = "server error"
    elif error_type == "APITimeoutError":
        reason = "timeout"
    elif error_type == "APIConnectionError":
        reason = "connection issue"
    elif error_type == "APIEmptyResponseError":
        reason = "empty response"
    else:
        reason = error_type or "error"
    wait_str = format_elapsed(float(wait)) if wait else "0s"
    # handle dict fallback
    if isinstance(retry, dict):
        reason = retry.get("reason") or reason
        wait_str = format_elapsed(float(retry.get("wait_s", 0) or 0))
        next_attempt = retry.get("next_attempt", next_attempt)
        max_attempts = retry.get("max_attempts", max_attempts)
    return Text(
        f"Retrying after {reason} · attempt {next_attempt}/{max_attempts} · {wait_str}",
        style="grey50 italic",
    )


__all__ = [
    "_ContentBlock",
    "ContentBlock",
    "_NotificationBlock",
    "NotificationBlock",
    "_StatusBlock",
    "StatusBlock",
    "_TodoBlock",
    "TodoBlock",
    "Notification",
    "StatusUpdate",
    "MAX_LIVE_NOTIFICATIONS",
    "format_token_count",
    "format_context_status",
    "_estimate_tokens",
    "_find_committed_boundary",
    "_bullet_frame_for",
    "_format_step_retry",
]
