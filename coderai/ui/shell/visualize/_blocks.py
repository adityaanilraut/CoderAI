# Ported from coderai/cli/stream_blocks.py - kimi structure (ui/shell/visualize/_blocks.py).
"""Live visualizer blocks for streaming and progress output.

Single Live(Group, transient, vertical_overflow=visible) handles streaming
markdown commitment, thinking pulses, and status/notification blocks.
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, NamedTuple, cast

import streamingjson
from kosong.tooling import BriefDisplayBlock, ToolReturnValue
from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.spinner import Spinner
from rich.style import Style
from rich.text import Text

from coderai.tools import extract_key_argument
from coderai.tools.display import BackgroundTaskDisplayBlock, DiffDisplayBlock, TodoDisplayBlock
from coderai.utils.rich.diff_render import collect_diff_hunks, render_diff_panel, render_diff_summary_panel
from coderai.wire.types import ToolCall, ToolCallPart, ToolResult

# reuse CoderAI console (MANPAGER-safe, neutral theme)
try:
    from coderai.ui.shell.console import console  # type: ignore[assignment]
except Exception:  # fallback for tests
    from rich.console import Console

    console = Console()  # type: ignore[no-redef,assignment]

from coderai.utils.rich.columns import BulletColumns
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
    from coderai.utils.rich.syntax import KIMI_ANSI_THEME  # noqa: F401

    from rich.markdown import Markdown  # type: ignore[assignment]
except Exception:
    try:
        from rich.markdown import Markdown  # type: ignore[no-redef]
    except Exception:
        Markdown = None  # type: ignore[assignment,misc]


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
        if not boundary:
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


class _ToolCallBlock:
    class FinishedSubCall(NamedTuple):
        call: ToolCall
        result: ToolReturnValue

    def __init__(self, tool_call: ToolCall):
        self._tool_name = tool_call.function.name
        self._lexer = streamingjson.Lexer()
        if tool_call.function.arguments is not None:
            self._lexer.append_string(tool_call.function.arguments)

        self._argument = extract_key_argument(self._lexer, self._tool_name)
        self._full_url = self._extract_full_url(tool_call.function.arguments, self._tool_name)
        self._result: ToolReturnValue | None = None
        self._subagent_id: str | None = None
        self._subagent_type: str | None = None

        self._ongoing_subagent_tool_calls: dict[str, ToolCall] = {}
        self._last_subagent_tool_call: ToolCall | None = None
        self._n_finished_subagent_tool_calls = 0
        self._finished_subagent_tool_calls = deque[_ToolCallBlock.FinishedSubCall](
            maxlen=MAX_SUBAGENT_TOOL_CALLS_TO_SHOW
        )

        self._spinning_dots = Spinner("dots", text="")
        self._renderable: RenderableType = self._compose()

    def compose(self) -> RenderableType:
        return self._renderable

    @property
    def finished(self) -> bool:
        return self._result is not None

    def append_args_part(self, args_part: str):
        if self.finished:
            return
        self._lexer.append_string(args_part)
        argument = extract_key_argument(self._lexer, self._tool_name)
        if argument and argument != self._argument:
            self._argument = argument
            self._full_url = self._extract_full_url(self._lexer.complete_json(), self._tool_name)
            self._renderable = BulletColumns(
                self._build_headline_text(),
                bullet=self._spinning_dots,
            )

    def finish(self, result: ToolReturnValue):
        self._result = result
        self._renderable = self._compose()

    def append_sub_tool_call(self, tool_call: ToolCall):
        self._ongoing_subagent_tool_calls[tool_call.id] = tool_call
        self._last_subagent_tool_call = tool_call

    def append_sub_tool_call_part(self, tool_call_part: ToolCallPart):
        if self._last_subagent_tool_call is None:
            return
        if not tool_call_part.arguments_part:
            return
        if self._last_subagent_tool_call.function.arguments is None:
            self._last_subagent_tool_call.function.arguments = tool_call_part.arguments_part
        else:
            self._last_subagent_tool_call.function.arguments += tool_call_part.arguments_part

    def finish_sub_tool_call(self, tool_result: ToolResult):
        self._last_subagent_tool_call = None
        sub_tool_call = self._ongoing_subagent_tool_calls.pop(tool_result.tool_call_id, None)
        if sub_tool_call is None:
            return

        self._finished_subagent_tool_calls.append(
            _ToolCallBlock.FinishedSubCall(
                call=sub_tool_call,
                result=tool_result.return_value,
            )
        )
        self._n_finished_subagent_tool_calls += 1
        self._renderable = self._compose()

    def set_subagent_metadata(self, agent_id: str, subagent_type: str) -> None:
        changed = (self._subagent_id, self._subagent_type) != (agent_id, subagent_type)
        self._subagent_id = agent_id
        self._subagent_type = subagent_type
        if changed:
            self._renderable = self._compose()

    def _compose(self) -> RenderableType:
        lines: list[RenderableType] = [
            self._build_headline_text(),
        ]
        if self._subagent_id is not None and self._subagent_type is not None:
            lines.append(
                BulletColumns(
                    Text(
                        f"subagent {self._subagent_type} ({self._subagent_id})",
                        style="grey50",
                    ),
                    bullet_style="grey50",
                )
            )

        if self._n_finished_subagent_tool_calls > MAX_SUBAGENT_TOOL_CALLS_TO_SHOW:
            n_hidden = self._n_finished_subagent_tool_calls - MAX_SUBAGENT_TOOL_CALLS_TO_SHOW
            lines.append(
                BulletColumns(
                    Text(
                        f"{n_hidden} more tool call{'s' if n_hidden > 1 else ''} ...",
                        style="grey50 italic",
                    ),
                    bullet_style="grey50",
                )
            )
        for sub_call, sub_result in self._finished_subagent_tool_calls:
            argument = extract_key_argument(
                sub_call.function.arguments or "", sub_call.function.name
            )
            sub_url = self._extract_full_url(sub_call.function.arguments, sub_call.function.name)
            sub_text = Text()
            sub_text.append("Used ")
            sub_text.append(sub_call.function.name, style="blue")
            if argument:
                sub_text.append(" (", style="grey50")
                arg_style = Style(color="grey50", link=sub_url) if sub_url else "grey50"
                sub_text.append(argument, style=arg_style)
                sub_text.append(")", style="grey50")
            lines.append(
                BulletColumns(
                    sub_text,
                    bullet_style="green" if not sub_result.is_error else "dark_red",
                )
            )

        if self._result is not None:
            display = self._result.display
            idx = 0
            while idx < len(display):
                block = display[idx]
                if isinstance(block, DiffDisplayBlock):
                    path = block.path
                    diff_blocks: list[DiffDisplayBlock] = []
                    while idx < len(display):
                        b = display[idx]
                        if not isinstance(b, DiffDisplayBlock) or b.path != path:
                            break
                        diff_blocks.append(b)
                        idx += 1
                    if any(b.is_summary for b in diff_blocks):
                        lines.append(render_diff_summary_panel(path, diff_blocks))
                    else:
                        hunks, added_total, removed_total = collect_diff_hunks(diff_blocks)
                        if hunks:
                            lines.append(render_diff_panel(path, hunks, added_total, removed_total))
                elif isinstance(block, BriefDisplayBlock):
                    style = "grey50" if not self._result.is_error else "dark_red"
                    if block.text:
                        lines.append(Text(block.text.rstrip("\n"), style=style))
                    idx += 1
                elif isinstance(block, TodoDisplayBlock):
                    markdown = self._render_todo_markdown(block)
                    if markdown:
                        lines.append(Markdown(markdown, style="grey50"))
                    idx += 1
                elif isinstance(block, BackgroundTaskDisplayBlock):
                    lines.append(
                        Markdown(
                            (f"`{block.task_id}` [{block.status}] {block.description}"),
                            style="grey50",
                        )
                    )
                    idx += 1
                else:
                    idx += 1

        if self.finished:
            assert self._result is not None
            return BulletColumns(
                Group(*lines),
                bullet_style="green" if not self._result.is_error else "dark_red",
            )
        else:
            return BulletColumns(
                Group(*lines),
                bullet=self._spinning_dots,
            )

    @staticmethod
    def _extract_full_url(arguments: str | None, tool_name: str) -> str | None:
        if tool_name != "FetchURL" or not arguments:
            return None
        try:
            args = json.loads(arguments, strict=False)
        except (json.JSONDecodeError, TypeError):
            return None
        if isinstance(args, dict):
            url = cast(dict[str, Any], args).get("url")
            if url:
                return str(url)
        return None

    def _build_headline_text(self) -> Text:
        text = Text()
        text.append("Used " if self.finished else "Using ")
        text.append(self._tool_name, style="blue")
        if self._argument:
            text.append(" (", style="grey50")
            arg_style = Style(color="grey50", link=self._full_url) if self._full_url else "grey50"
            text.append(self._argument, style=arg_style)
            text.append(")", style="grey50")
        return text

    def _render_todo_markdown(self, block: TodoDisplayBlock) -> str:
        lines: list[str] = []
        for todo in block.items:
            normalized = todo.status.replace("_", " ").lower()
            match normalized:
                case "pending":
                    lines.append(f"- {todo.title}")
                case "in progress":
                    lines.append(f"- {todo.title} ←")
                case "done":
                    lines.append(f"- ~~{todo.title}~~")
                case _:
                    lines.append(f"- {todo.title}")
        return "\n".join(lines)


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




# --- from coderai/cli/thinking.py ---
"""Thinking mode visual hierarchy and reasoning styling.

Canonical reasoning rendering lives here; live streaming pulse is
now unified in coderai.ui.shell.visualize._blocks._ContentBlock(is_think=True).
This module keeps the stable public API (summarize_thinking,
render_thinking_block) and retains LiveThinkingStreamer as legacy
fallback for non-Rich / non-TTY paths (raw \\r). New live code should
use stream_blocks.
"""


import re
import shutil
import sys
import time
from typing import Any

from rich.console import Console
from rich.markup import escape

from coderai.cli.elapsed import bullet_frame_for, format_elapsed


def summarize_thinking(thinking_text: str, max_chars: int = 140) -> str:
    """Extract a concise one-line summary from a reasoning block."""
    cleaned = re.sub(r"\s+", " ", thinking_text).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    if max_chars <= 3:
        return cleaned[:max_chars]
    return cleaned[: max_chars - 3] + "..."


def render_thinking_block(
    console: Any | None,
    thinking_text: str,
    elapsed_seconds: float | None = None,
    expanded: bool = False,
    token_count: int | None = None,
) -> None:
    """Render a clean, compact thinking mode block or expanded reasoning trace."""
    if not thinking_text.strip():
        return

    if elapsed_seconds is not None:
        elapsed_str = f"{elapsed_seconds:.1f}s"
        if elapsed_seconds >= 60:
            elapsed_str = format_elapsed(elapsed_seconds)
    else:
        elapsed_str = ""
    duration_str = f" [dim cyan]({elapsed_str})[/]" if elapsed_str else ""
    tok_str = f" [dim]· {token_count} tokens[/]" if token_count else ""
    rate_str = ""
    if elapsed_seconds and elapsed_seconds > 0.5 and token_count:
        rate_str = f" [dim]({int(token_count / elapsed_seconds)} tok/s)[/]"

    active_console = console or Console()
    if expanded:
        active_console.print(
            f"  [bold magenta]● Reasoning Trace[/]{duration_str}{tok_str}{rate_str}"
        )
        for line in thinking_text.strip().splitlines()[:25]:
            active_console.print(f"    [dim italic]{escape(line)}[/]")
        if len(thinking_text.strip().splitlines()) > 25:
            active_console.print(
                f"    [dim]... ({len(thinking_text.strip().splitlines()) - 25} more lines truncated)[/]"
            )
    else:
        # Collapsed: stats only, no thought-content summary. Dumping the raw
        # reasoning text here leaked internal chain-of-thought into the chat
        # (e.g. "The user just said hi...") on every turn. Full traces remain
        # available via expanded mode (/thinking full).
        active_console.print(f"  [dim]● Reasoning[/]{duration_str}{tok_str}")


class LiveThinkingStreamer:
    """Legacy live visualizer (raw \\r) — kept for non-Rich fallback.

    Unified path uses stream_blocks._ContentBlock(is_think=True) with
    Live(Group, transient). This shim is retained for test compat and
    non-TTY fallback; it delegates token math to elapsed but keeps the
    raw ANSI line for width-bound checks.
    """

    SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    BULLET_FRAMES = (".  ", ".. ", "...", " ..", "  .", "   ")

    def __init__(self, console: Any | None = None) -> None:
        self.console = console
        self.thinking_chunks: list[str] = []
        self.start_time: float | None = None
        self.is_active: bool = False
        self.frame_idx: int = 0
        self._last_render_time: float = 0.0
        self._last_line_len: int = 0
        # optional canonical block for tok/s parity check
        try:

            self._block = _ContentBlock(is_think=True)
        except Exception:
            self._block = None  # type: ignore

    def _is_tty(self) -> bool:
        if self.console is not None and hasattr(self.console, "is_terminal"):
            val = getattr(self.console, "is_terminal")
            if isinstance(val, bool):
                return val
        return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

    def _get_term_width(self) -> int:
        if (
            self.console is not None
            and isinstance(getattr(self.console, "width", None), int)
            and self.console.width > 0
        ):
            return self.console.width
        return shutil.get_terminal_size(fallback=(80, 24)).columns

    def on_chunk(self, chunk: str) -> None:
        if not chunk:
            return
        if self.start_time is None:
            self.start_time = time.time()
            self.is_active = True
        self.thinking_chunks.append(chunk)
        if self._block is not None:
            try:
                self._block.append(chunk)
            except Exception:
                pass
        now = time.time()
        if now - self._last_render_time > 0.06:
            self._render_inline()
            self._last_render_time = now

    def _render_inline(self) -> None:
        if not self.is_active or not self.start_time:
            return
        if not self._is_tty():
            return
        # Never raw-write while a prompt_toolkit app owns the screen:
        # patch_stdout converts \r rewrites into stacked lines (one spinner
        # frame per line) instead of a single updating line.
        try:
            from prompt_toolkit.application.current import get_app_or_none

            _app = get_app_or_none()
            if _app is not None and bool(getattr(_app, "is_running", False)):
                return
        except Exception:
            pass
        elapsed = time.time() - self.start_time
        frame = self.SPINNER_FRAMES[self.frame_idx % len(self.SPINNER_FRAMES)]
        self.frame_idx += 1
        bullet = bullet_frame_for(elapsed)
        elapsed_fmt = format_elapsed(elapsed)
        term_width = max(30, self._get_term_width())
        elapsed_str = f"({elapsed_fmt})"
        # Clean single-line status: spinner + elapsed only. Thought content,
        # token counts and tok/s are deliberately withheld here — they leaked
        # internal reasoning into the live view and pushed the line past the
        # terminal width, which wrapped and left stacked spinner lines behind.
        visible = f"  {frame} Reasoning{bullet} {elapsed_str}"
        if len(visible) > term_width:
            visible = visible[: max(0, term_width - 3)] + "..."
            line = f"\r\x1b[2K{visible}"
        else:
            line = (
                f"\r\x1b[2K  \x1b[35m\x1b[1m{frame}\x1b[0m"
                f" \x1b[1;35mReasoning{bullet}\x1b[0m"
                f" \x1b[36m{elapsed_str}\x1b[0m"
            )
        # Pad to overwrite any previously rendered longer line.
        pad = max(0, self._last_line_len - len(visible))
        if pad:
            line += " " * pad + f"\x1b[{pad}D"
        sys.stdout.write(line)
        sys.stdout.flush()
        self._last_line_len = len(visible)

    def finalize(self, console: Any | None = None, expanded: bool = False) -> str:
        if not self.is_active or not self.thinking_chunks:
            self.reset()
            return ""
        elapsed = (time.time() - self.start_time) if self.start_time else None
        full_thinking = "".join(self.thinking_chunks).strip()
        from coderai.cli.elapsed import estimate_tokens

        tok_count = estimate_tokens(full_thinking) if full_thinking else None
        if self._is_tty() and self.is_active:
            sys.stdout.write("\r\x1b[2K\r")
            sys.stdout.flush()
        active_console = console or self.console
        render_thinking_block(
            active_console,
            full_thinking,
            elapsed_seconds=elapsed,
            expanded=expanded,
            token_count=tok_count,
        )
        self.reset()
        return full_thinking

    def reset(self) -> None:
        if self._is_tty() and self.is_active:
            try:
                sys.stdout.write("\r\x1b[2K\r")
                sys.stdout.flush()
            except Exception:
                pass
        self.thinking_chunks.clear()
        self.start_time = None
        self.is_active = False
        self.frame_idx = 0
        self._last_render_time = 0.0
        self._last_line_len = 0
        if self._block is not None:
            try:

                self._block = _ContentBlock(is_think=True)
            except Exception:
                pass


# --- from coderai/cli/tool_card.py ---
"""Formatted tool-result cards for CLI tool executions."""


import json
from typing import Any


from rich.markup import escape

from coderai.utils.rich.diff_render import render_diff_preview
from coderai.soul.session.manager import SessionMessage

_RICH = True


def parse_tool_message(message: SessionMessage) -> tuple[str, str, bool, dict[str, Any] | None]:
    """Parse tool result message JSON payload into (tool_name, summary, is_ok, metadata)."""
    content = message.content or ""
    metadata: dict[str, Any] | None = None
    try:
        result = json.loads(content)
        name = str(result.get("name") or "tool")
        ok = result.get("ok") is not False
        if isinstance(result.get("metadata"), dict):
            metadata = result["metadata"]

        if not ok:
            err = str(result.get("error", "failed"))
            return name, f"failed: {err[:120]}", False, metadata

        output = result.get("output")
        if isinstance(output, str):
            first_line = output.splitlines()[0] if output.splitlines() else "(no output)"
            return name, first_line[:120], True, metadata
        return name, "completed", True, metadata
    except (ValueError, TypeError):
        return "tool", content[:120], True, metadata


def _render_collapsible_block(
    console: Any,
    lines: list[str],
    preview_limit: int = 8,
    indent: str = "      [dim]│[/] ",
) -> None:
    """Render lines with collapsible preview (changed-lines-only style).

    Shows first preview_limit lines + hint for remaining (Kimi-style).
    """
    if not lines:
        return
    shown = lines[:preview_limit]
    for line in shown:
        # Escape unless already styled
        text = line if line.startswith("[") and line.endswith("]") else escape(line)
        console.print(f"{indent}{text}")
    remaining = len(lines) - len(shown)
    if remaining > 0:
        console.print(f"      [dim italic]... {remaining} more lines (press Enter to expand)[/]")


def _render_bash_card(
    console: Any,
    output_text: str | None,
    error_text: str | None,
    metadata: dict[str, Any],
    ok: bool,
) -> None:
    """Render a compact terminal command output event with collapsible streaming."""
    cmd = metadata.get("command") or ""
    exit_code = metadata.get("exit_code") if "exit_code" in metadata else (0 if ok else 1)
    status_style = "bold green" if ok else "bold red"
    status_text = f"exit {exit_code}" if exit_code is not None else ("ok" if ok else "failed")
    elapsed = metadata.get("duration_ms") or metadata.get("elapsed_ms")
    elapsed_str = f" [dim]({elapsed:.0f}ms)[/]" if isinstance(elapsed, (int, float)) else ""

    header_text = (
        f"    ↳ [bold cyan]$ {escape(cmd)}[/] [{status_style}]({status_text})[/]{elapsed_str}"
        if cmd
        else f"    ↳ [{status_style}]Shell Output ({status_text})[/]{elapsed_str}"
    )

    if console is not None and _RICH:
        # Grouped block with side border (Kimi-style)

        console.print(header_text)
        content_lines: list[str] = []
        if output_text and output_text.strip():
            content_lines.extend(output_text.strip().splitlines())
        if error_text and error_text.strip():
            content_lines.extend(
                f"[bold red]Error:[/] {escape(line)}" for line in error_text.strip().splitlines()
            )

        if content_lines:
            _render_collapsible_block(console, content_lines, preview_limit=12)
    else:
        plain_header = (
            f"    ↳ $ {cmd} ({status_text})" if cmd else f"    ↳ Shell Output ({status_text})"
        )
        print(plain_header)
        if output_text and output_text.strip():
            for line in output_text.strip().splitlines()[:20]:
                print(f"      | {line}")
        if error_text and error_text.strip():
            print(f"      Error: {error_text.strip()}")


def _render_search_card(console: Any, output_text: str | None, metadata: dict[str, Any]) -> None:
    """Render compact web search results matching deepseek-harness webCardModel presentation."""
    raw_results = metadata.get("results") or []
    sources: list[dict[str, Any]] = []
    queries: list[str] = []
    seen_urls: set[str] = set()

    for item in raw_results:
        if isinstance(item, dict):
            q = item.get("query")
            if q and q not in queries:
                queries.append(q)
            for src in item.get("sources") or []:
                if isinstance(src, dict) and src.get("url") and src["url"] not in seen_urls:
                    seen_urls.add(src["url"])
                    sources.append(src)
        elif isinstance(item, dict) and "url" in item and item.get("url"):
            if item["url"] not in seen_urls:
                seen_urls.add(item["url"])
                sources.append(item)

    if not sources and isinstance(metadata.get("sources"), list):
        for src in metadata["sources"]:
            if isinstance(src, dict) and src.get("url") and src["url"] not in seen_urls:
                seen_urls.add(src["url"])
                sources.append(src)

    query_title = metadata.get("query") or (", ".join(queries) if queries else "")

    if console is not None and _RICH:
        title = (
            f'    ↳ [bold cyan]Web Search:[/] [bold yellow]"{query_title}"[/]'
            if query_title
            else "    ↳ [bold cyan]Web Search Results[/]"
        )
        console.print(title)
        for idx, src in enumerate(sources[:6], 1):
            s_title = src.get("title") or src.get("url") or "Source"
            s_url = src.get("url") or ""
            snippet = src.get("snippet") or ""
            date_str = (
                f" [dim cyan]({src.get('publishedAt') or src.get('published_at')})[/]"
                if (src.get("publishedAt") or src.get("published_at"))
                else ""
            )
            console.print(
                f"      [bold cyan]{idx}.[/] [bold]{s_title}[/]{date_str} [dim]•[/] [dim cyan]{s_url}[/]"
            )
            if snippet:
                snip_short = snippet[:120] + "..." if len(snippet) > 120 else snippet
                console.print(f"         [dim]{snip_short}[/]")
    elif sources:
        print(f"    ↳ Web Search: {query_title or 'Results'}")
        for idx, src in enumerate(sources[:6], 1):
            print(f"      {idx}. {src.get('title') or src.get('url')} - {src.get('url')}")


def _render_fetch_card(
    console: Any,
    output_text: str | None,
    error_text: str | None,
    metadata: dict[str, Any],
    ok: bool,
) -> None:
    """Render a compact WebFetch result event."""
    url = metadata.get("url") or ""
    status_code = metadata.get("status_code") or metadata.get("status") or (200 if ok else 400)
    bytes_count = (
        metadata.get("bytes")
        or metadata.get("content_length")
        or (len(output_text.encode("utf-8")) if output_text else 0)
    )

    try:
        sc_num = int(status_code)
    except (ValueError, TypeError):
        sc_num = 200 if ok else 400

    status_style = "bold green" if (200 <= sc_num < 300) else "bold red"
    size_kb = bytes_count / 1024.0
    size_str = f"{size_kb:.1f} KB" if size_kb >= 1.0 else f"{bytes_count} B"

    if console is not None and _RICH:
        console.print(
            f"    ↳ [bold cyan]WebFetch[/] [{status_style}][{status_code}][/] [dim]({size_str})[/] • [dim]{url}[/]"
        )
        if output_text and output_text.strip():
            preview_lines = output_text.strip().splitlines()[:5]
            for pl in preview_lines:
                console.print(f"      [dim]│[/] {pl[:100]}")
    else:
        print(f"    ↳ WebFetch [{status_code}] ({size_str}) - {url}")


def _render_read_card(
    console: Any, file_path: str, metadata: dict[str, Any], output_text: str | None
) -> None:
    """Render a compact file slice inspection event with syntax highlighting."""
    snip_id = metadata.get("snippet_id")
    lines_cnt = metadata.get("line_count")
    offset = metadata.get("offset", 1)
    range_str = f"L{offset}-L{offset + lines_cnt - 1}" if lines_cnt else ""
    target_str = f"[bold cyan]{file_path}[/]" if file_path else "File Read"

    badges = []
    if range_str:
        badges.append(f"[bold yellow]{range_str}[/]")
    if lines_cnt:
        badges.append(f"[dim]{lines_cnt} lines[/]")
    if snip_id:
        badges.append(f"[bold magenta]snippet:{snip_id}[/]")

    badge_info = f" ({', '.join(badges)})" if badges else ""
    title = f"    ↳ {target_str}{badge_info}"

    if console is not None and _RICH:
        console.print(title)
        if output_text and output_text.strip():
            lines = output_text.strip().splitlines()
            display_limit = 15
            # Try syntax highlighting per file extension
            try:
                from coderai.utils.rich.syntax import KimiSyntax

                ext = file_path.rsplit(".", 1)[-1] if "." in file_path else "text"
                # Highlight as a block for better token colors
                code = "\n".join(lines[:display_limit])
                syntax = KimiSyntax(
                    code, ext, theme="kimi-ansi", line_numbers=True, start_line=offset or 1
                )
                console.print(syntax)
            except Exception:
                for idx, line in enumerate(lines[:display_limit]):
                    line_no = (offset or 1) + idx
                    console.print(f"      [dim]{line_no:>4} │[/] {escape(line)}")
            if len(lines) > display_limit:
                console.print(
                    f"      [dim italic]... ({len(lines) - display_limit} more lines hidden — press Enter to expand)[/]"
                )
    else:
        print(
            f"    ↳ Read: {file_path} ({lines_cnt or len(output_text.splitlines()) if output_text else 0} lines)"
        )


def _render_search_grep_card(
    console: Any, output_text: str | None, metadata: dict[str, Any], ok: bool
) -> None:
    """Render grep / glob code search matches cleanly."""
    query = metadata.get("query") or metadata.get("pattern") or ""
    path = metadata.get("path") or metadata.get("directory") or ""
    matches_count = metadata.get("matches_count") or (
        len(output_text.splitlines()) if output_text else 0
    )

    title = f"    ↳ [bold cyan]Search:[/] [bold yellow]'{escape(query)}'[/]"
    if path:
        title += f" in [dim]{escape(path)}[/]"
    title += f" [dim]({matches_count} matches)[/]"

    if console is not None and _RICH:
        console.print(title)
        if output_text and output_text.strip():
            lines = output_text.strip().splitlines()
            for line in lines[:15]:
                console.print(f"      [dim]│[/] {escape(line)}")
            if len(lines) > 15:
                console.print(f"      [dim]... ({len(lines) - 15} more matches hidden)[/]")
    elif output_text:
        print(f"    ↳ Search '{query}': {matches_count} matches")




def _render_subagent_card(
    console: Any, output_text: str | None, metadata: dict[str, Any], ok: bool
) -> None:
    """Render delegated subagent task execution cleanly."""
    task_name = metadata.get("task_name") or metadata.get("task") or "Subagent Task"
    agent_id = metadata.get("agent_id") or metadata.get("id") or ""
    status = "completed" if ok else "failed"
    status_style = "bold green" if ok else "bold red"

    title = f"    ↳ [bold magenta]Subagent:[/] [white]{escape(task_name)}[/] [{status_style}]({status})[/]"
    if agent_id:
        title += f" [dim]id:{agent_id[:8]}[/]"

    if console is not None and _RICH:
        console.print(title)
        if output_text and output_text.strip():
            lines = output_text.strip().splitlines()[:10]
            for line in lines:
                console.print(f"      [dim]│[/] {escape(line)}")
    elif output_text:
        print(f"    ↳ Subagent {task_name}: {status}")


def _render_session_card(
    console: Any, output_text: str | None, metadata: dict[str, Any], ok: bool
) -> None:
    """Render session query / trace results cleanly."""
    query = metadata.get("query") or metadata.get("session_id") or ""
    results_count = metadata.get("count") or (len(output_text.splitlines()) if output_text else 0)
    status_style = "bold green" if ok else "bold red"

    title = (
        f"    ↳ [bold cyan]Session Query:[/] [bold yellow]'{escape(query)}'[/] [{status_style}]({results_count} events)[/]"
        if query
        else f"    ↳ [bold cyan]Session Query[/] [{status_style}]({results_count} events)[/]"
    )

    if console is not None and _RICH:
        console.print(title)
        if output_text and output_text.strip():
            for line in output_text.strip().splitlines()[:10]:
                console.print(f"      [dim]│[/] {escape(line)}")
    elif output_text:
        print(f"    ↳ Session Query: {results_count} events")




def render_tool_card(console: Any | None, message: SessionMessage) -> None:
    """Render a compact sequential tool result event with grouped blocks, collapsible output, and status."""
    name, summary_text, ok, metadata = parse_tool_message(message)
    # Kimi-style bullet: green dot ok, dark_red error, with spinner semantics
    bullet = "[bold green]●[/]" if ok else "[bold red]✗[/]"

    raw_output: str | None = None
    raw_error: str | None = None
    try:
        parsed_payload = json.loads(message.content or "{}")
        raw_output = parsed_payload.get("output")
        raw_error = parsed_payload.get("error")
    except Exception:
        pass

    if console is not None and _RICH:
        # Display main tool status line
        console.print(f"  {bullet} [bold cyan]{name}[/] [dim]•[/] [white]{escape(summary_text)}[/]")

        # Tool-specific compact events
        if metadata:
            file_path = metadata.get("file_path") or metadata.get("target_path") or ""

            # Diff preview for Edit / Write
            diff_text = metadata.get("diff_preview")
            if isinstance(diff_text, str) and diff_text.strip():
                card_title = f"{name}: {file_path}" if file_path else f"{name} Changes"
                render_diff_preview(console, diff_text, title=card_title)

            # Plan / Todo preview for UpdatePlan / todo_write / SetTodoList
            todos_data = metadata.get("todos")
            plan_text = metadata.get("plan")
            if name in (
                "todo_write",
                "SetTodoList",
                "set_todo_list",
                "UpdatePlan",
                "update_plan",
                "write_plan",
            ):
                if todos_data and isinstance(todos_data, list):
                    render_todo_list(console, todos_data, title="Todo")
                elif isinstance(plan_text, str) and plan_text.strip():
                    render_todo_list(console, plan_text, title="Todo")

            # Bash tool card
            if name in ("bash", "Bash", "terminal"):
                _render_bash_card(console, raw_output, raw_error, metadata, ok)

            # WebSearch tool card
            elif name in ("WebSearch", "web_search"):
                _render_search_card(console, raw_output, metadata)

            # WebFetch tool card
            elif name in ("WebFetch", "web_fetch", "fetch"):
                _render_fetch_card(console, raw_output, raw_error, metadata, ok)

            # Code search / grep / glob card
            elif name in ("grep", "glob", "file_search", "find_files"):
                _render_search_grep_card(console, raw_output, metadata, ok)


            # Subagent task card
            elif name in ("subagent", "delegate", "agent_task", "invoke_agent"):
                _render_subagent_card(console, raw_output, metadata, ok)

            # Session query / trace card
            elif name in (
                "session_query",
                "session_search",
                "session_trace",
                "session_event_search",
                "session_event_read",
            ):
                _render_session_card(console, raw_output, metadata, ok)


            # Read tool snippet info
            elif name in ("read", "Read", "view_file"):
                _render_read_card(console, file_path, metadata, raw_output)
    else:
        mark = "✓" if ok else "✗"
        print(f"  {mark} {name}: {summary_text}")
        if metadata:
            diff_text = metadata.get("diff_preview")
            if isinstance(diff_text, str) and diff_text.strip():
                render_diff_preview(None, diff_text, title=f"{name} Changes")
            todos_data = metadata.get("todos")
            plan_text = metadata.get("plan")
            if name in (
                "todo_write",
                "SetTodoList",
                "set_todo_list",
                "UpdatePlan",
                "update_plan",
                "write_plan",
            ):
                if todos_data and isinstance(todos_data, list):
                    render_todo_list(None, todos_data, title="Todo")
                elif isinstance(plan_text, str) and plan_text.strip():
                    render_todo_list(None, plan_text, title="Todo")


# --- from coderai/cli/plan_render.py ---
"""Plan checklist and Todo list renderer for Plan Mode, UpdatePlan, and todo_write tools."""


from dataclasses import dataclass
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.text import Text


@dataclass
class TodoItem:
    title: str
    status: str = "pending"  # "pending", "in_progress", "completed", "cancelled"


@dataclass
class TodoDisplayBlock:
    items: list[TodoItem]
    title: str = "Todo"


def parse_plan_stats(plan_text: str) -> tuple[int, int]:
    """Parse total and completed task count from markdown checklist."""
    total = 0
    completed = 0
    for line in plan_text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("- [x]", "* [x]", "- [X]", "* [X]")):
            total += 1
            completed += 1
        elif stripped.startswith(("- [ ]", "* [ ]")):
            total += 1
    return total, completed


def make_plan_progress_bar(completed: int, total: int, width: int = 10) -> str:
    """Generate a visual progress bar string for plans."""
    if total <= 0:
        return ""
    pct = max(0.0, min(1.0, completed / total))
    filled = int(round(pct * width))
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {int(pct * 100)}%"


def format_todo_item(title: str, status: str = "pending", indent: str = "") -> Text:
    """Format an individual todo item with modern icons and styling."""
    t = Text()
    normalized = status.strip().lower().replace(" ", "_")
    if normalized in ("in_progress", "active", "doing"):
        t.append(f"{indent}● ", style="bold #38bdf8")
        t.append(f"{title}\n", style="bold white")
    elif normalized in ("completed", "done", "finished"):
        t.append(f"{indent}✓ ", style="bold #4ade80")
        t.append(f"{title}\n", style="dim green")
    elif normalized in ("cancelled", "canceled", "skipped"):
        t.append(f"{indent}- ", style="dim yellow")
        t.append(f"{title}\n", style="dim yellow")
    else:  # pending
        t.append(f"{indent}○ ", style="bold #64748b")
        t.append(f"{title}\n", style="white")
    return t


def format_todo_content(todos: list[dict[str, Any]] | list[TodoItem] | str) -> Text:
    """Format structured todos or markdown checklist into styled Rich Text."""
    if isinstance(todos, str):
        return format_plan_content(todos)

    formatted = Text()
    for item in todos:
        if isinstance(item, TodoItem):
            title = item.title
            status = item.status
        elif isinstance(item, dict):
            title = str(item.get("content") or item.get("title") or "").strip()
            status = str(item.get("status") or "pending")
        else:
            continue
        if not title:
            continue
        formatted.append(format_todo_item(title, status))
    return formatted


def format_plan_content(plan_text: str) -> Text:
    """Format markdown checklist into styled Rich Text while preserving nested hierarchy."""
    formatted = Text()
    lines = plan_text.splitlines()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            formatted.append("\n")
            continue

        # Calculate indentation depth (preserving sub-task nesting)
        leading_spaces = len(line) - len(line.lstrip(" "))
        indent_depth = max(0, leading_spaces // 2)
        base_indent = "  " * indent_depth

        if stripped.startswith(("- [x]", "* [x]", "- [X]", "* [X]")):
            item_text = stripped[5:].strip()
            formatted.append(format_todo_item(item_text, "completed", indent=base_indent))
        elif stripped.startswith(("- [>]", "* [>]", "- [*]", "* [*]")):
            item_text = stripped[5:].strip()
            formatted.append(format_todo_item(item_text, "in_progress", indent=base_indent))
        elif stripped.startswith(("- [-]", "* [-]")):
            item_text = stripped[5:].strip()
            formatted.append(format_todo_item(item_text, "cancelled", indent=base_indent))
        elif stripped.startswith(("- [ ]", "* [ ]")):
            item_text = stripped[5:].strip()
            formatted.append(format_todo_item(item_text, "pending", indent=base_indent))
        elif stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            formatted.append(f"\n{base_indent}{heading}\n", style="bold #38bdf8")
        elif stripped.startswith(("-", "*", "•")):
            item_text = stripped.lstrip("-*• ").strip()
            formatted.append(f"{base_indent}• ", style="dim #64748b")
            formatted.append(f"{item_text}\n", style="dim")
        else:
            formatted.append(f"{base_indent}{stripped}\n", style="dim")

    return formatted


def create_todo_block(
    todos: list[dict[str, Any]] | list[TodoItem] | str,
    title: str = "Todo",
    term_width: int = 80,
) -> RenderableType:
    """Create a self-contained Rich Renderable for the Todo list with sleek rules and styling."""
    content = format_todo_content(todos)
    if not str(content).strip():
        return Text("")

    header_text = Text()
    header_text.append(title, style="bold #38bdf8")

    elements: list[RenderableType] = [
        header_text,
        content,
    ]
    return Group(*elements)


def render_todo_list(
    console: Any | None,
    todos: list[dict[str, Any]] | list[TodoItem] | str,
    title: str = "Todo",
) -> None:
    """Render the gold-standard Todo list directly to the terminal."""
    active_console = console or Console()
    width = getattr(active_console, "width", 80) or 80
    block = create_todo_block(todos, title=title, term_width=width)
    active_console.print(block)


def render_plan_preview(
    console: Any | None,
    plan_text: str,
    title: str = "Todo",
) -> None:
    """Render the plan progress checklist as a clean gold-standard Todo event."""
    if not plan_text.strip():
        return
    render_todo_list(console, plan_text, title=title)


# --- from coderai/cli/progress.py ---
"""Progress & status indicators — ported from Kimi CLI visualize/_blocks.py + utils/rich.

Provides:
- Dynamic spinners (dots/moon/balloon) with elapsed ticker
- Multi-step progress bar
- Live status badges (Thinking..., Searching..., Executing...)
Pure CLI.
"""


import time
from typing import Any

import signal

from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

from coderai.cli.elapsed import bullet_frame_for, format_elapsed, format_progress_bar


def _install_sigwinch(handler) -> None:  # type: ignore[no-untyped-def]
    try:

        def _wrapped(*args: Any) -> None:
            try:
                handler(*args)
            except Exception:
                pass

        sigwinch = getattr(signal, "SIGWINCH", None)
        if sigwinch is not None:
            signal.signal(sigwinch, _wrapped)
    except Exception:
        pass


def _reset_live_shape(live: Live | None) -> None:
    """Clear cached Live height so next refresh re-anchors after pager or resize (Kimi _live_view.py:173)."""
    if live is None:
        return
    try:
        if hasattr(live, "_live_render") and hasattr(live._live_render, "_shape"):  # type: ignore[attr-defined]
            live._live_render._shape = None  # type: ignore[attr-defined]
    except Exception:
        pass


class StatusSpinner:
    """Wraps rich Spinner with elapsed ticker.

    Mirrors Kimi _ContentBlock spinner: 'Composing... X s · N tokens'
    """

    def __init__(self, message: str = "Working", spinner: str = "dots") -> None:
        self.message = message
        self.spinner_name = spinner
        self._start = time.time()
        self._live: Live | None = None
        self._console: Console | None = None

    def start(self, console: Console | None = None) -> None:
        self._console = console or Console()
        self._start = time.time()
        try:
            spinner = Spinner(self.spinner_name, text=f"{self.message}...")
            self._live = Live(
                spinner,
                console=self._console,
                transient=True,
                refresh_per_second=10,
                vertical_overflow="visible",
            )
            self._live.start()

            def _on_resize(*_args: Any) -> None:
                if self._live:
                    _reset_live_shape(self._live)
                    self._live.refresh()

            _install_sigwinch(_on_resize)
        except Exception:
            self._live = None

    def update(self, message: str | None = None, token_count: int | None = None) -> None:
        if message:
            self.message = message
        if not self._live:
            return
        elapsed = time.time() - self._start
        bullet = bullet_frame_for(elapsed)
        elapsed_str = format_elapsed(elapsed)
        tok_str = f" · {token_count} tokens" if token_count else ""
        text = f"{self.message}{bullet}  {elapsed_str}{tok_str}"
        try:
            self._live.update(Spinner(self.spinner_name, text=text))
        except Exception:
            pass

    def stop(self) -> None:
        if self._live:
            try:
                self._live.update(Text(""))
                _reset_live_shape(self._live)
                self._live.stop()
            except Exception:
                pass
            self._live = None

    def pause_for_pager(self, pager_fn) -> None:  # type: ignore[no-untyped-def]
        """Ctrl-E pager hook: Live.stop → pager → Live.start with shape reset (Kimi 188)."""
        if not self._live:
            try:
                pager_fn()
            except Exception:
                pass
            return
        try:
            self._live.stop()
        except Exception:
            pass
        try:
            _reset_live_shape(self._live)
            pager_fn()
        finally:
            try:
                self._live.start()
                self._live.refresh()
            except Exception:
                pass


class MultiStepProgress:
    """Multi-step progress bar with elapsed ticker.

    Example:
        prog = MultiStepProgress(total=5, title="Implementing features")
        prog.start()
        prog.advance("Done parsing")
        ...
        prog.finish()
    """

    def __init__(self, total: int, title: str = "Progress", console: Console | None = None) -> None:
        self.total = total
        self.title = title
        self.completed = 0
        self.console = console or Console()
        self._start = time.time()
        self._live: Live | None = None
        self._current_step: str = ""

    def start(self) -> None:
        self._start = time.time()
        try:
            self._live = Live(
                self._render(), console=self.console, transient=False, refresh_per_second=4
            )
            self._live.start()
            _install_sigwinch(lambda *_: self._live and self._live.refresh())  # type: ignore[func-returns-value]
        except Exception:
            self._live = None

    def advance(self, step_msg: str = "") -> None:
        self.completed = min(self.completed + 1, self.total)
        self._current_step = step_msg
        if self._live:
            try:
                self._live.update(self._render())
            except Exception:
                pass
        else:
            try:
                self.console.print(self._render())
            except Exception:
                pass

    def _render(self) -> Text:
        elapsed = format_elapsed(time.time() - self._start)
        bar = format_progress_bar(self.completed, self.total)
        t = Text()
        t.append(f" {self.title} ", style="bold cyan")
        t.append(f"[{bar}]", style="dim")
        t.append(f" {elapsed}", style="dim cyan")
        if self._current_step:
            t.append(f"  {self._current_step}", style="dim")
        return t

    def finish(self, msg: str = "Done") -> None:
        if self._live:
            try:
                self._live.stop()
            except Exception:
                pass
            self._live = None
        elapsed = format_elapsed(time.time() - self._start)
        try:
            self.console.print(f"  [bold green]✓ {self.title}[/] [dim]({elapsed})[/] {msg}")
        except Exception:
            pass


# --- from coderai/cli/markdown_stream.py ---
"""Live token & markdown streaming renderer — shim over stream_blocks.

Re-exports canonical streaming helpers and provides MarkdownStreamRenderer
for progressive markdown rendering and formatting.
"""


import os
import signal
import time
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown as _RichMarkdown
from rich.style import Style
from rich.text import Text

from coderai.cli.elapsed import bullet_frame_for, format_elapsed

try:
    from coderai.utils.rich.syntax import KIMI_ANSI_THEME  # noqa: F401
except Exception:
    KIMI_ANSI_THEME = None  # type: ignore[assignment]

try:
    from coderai.cli.elapsed import estimate_tokens_float as _estimate_tokens_float  # type: ignore[import]
except Exception:
    from coderai.cli.elapsed import estimate_tokens as _estimate_tokens_float  # fallback int


def _strip_background(text: Text) -> Text:
    """Strip background styles from rendered text elements."""
    clean = Text(
        text.plain,
        justify=text.justify,
        overflow=text.overflow,
        no_wrap=text.no_wrap,
        end=text.end,
        tab_size=text.tab_size,
    )
    if text.style:
        base = text.style if isinstance(text.style, Style) else Style.parse(str(text.style))
        base = base.copy()
        if base._bgcolor is not None:
            base._bgcolor = None
        clean.stylize(base, 0, len(clean))
    for span in text.spans:
        st = span.style
        if st is None:
            continue
        ns = Style.parse(str(st)) if not isinstance(st, Style) else st.copy()
        if ns._bgcolor is not None:
            ns._bgcolor = None
        clean.stylize(ns, span.start, span.end)
    return clean


# Forked Markdown — uses _strip_background for headings/code when leak observed
def _heading_leaks_background() -> bool:
    if os.getenv("CODERAI_MARKDOWN_LEAK") == "1" or os.getenv("KIMI_MARKDOWN_LEAK") == "1":
        return True
    try:
        return False
    except Exception:
        return False


if _heading_leaks_background():

    class Markdown(_RichMarkdown):  # type: ignore[no-redef]
        """Forked Markdown with _strip_background support."""

        pass
else:
    Markdown = _RichMarkdown  # type: ignore[assignment,misc]

_md_parser: Any | None = None  # kept for import compat, unused


def _find_committed_boundary_heuristic(text: str) -> int:
    """Heuristic fallback — fence-aware rfind."""
    lines = text.split("\n")
    in_fence = False
    fence_char = ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            if not in_fence:
                in_fence = True
                fence_char = stripped[:3]
            elif stripped.startswith(fence_char):
                in_fence = False
            continue
        if in_fence:
            continue
    last_double = text.rfind("\n\n")
    if last_double != -1 and "\n" in text[last_double + 2 :]:
        return last_double + 2
    if in_fence:
        return 0
    last_nl = text.rfind("\n")
    if last_nl != -1:
        return last_nl + 1
    return 0


# ponytail: alias canonical parser before int-version shadows the name
_sb = _find_committed_boundary


def _find_committed_boundary_parser(text: str) -> int | None:
    """Delegate to canonical stream_blocks parser."""

    return _sb(text)


def _find_committed_boundary(text: str) -> int:
    """Find index up to which markdown is safe to flush (authoritative None->0)."""

    b = _sb(text)
    if b is not None:
        return b
    # fallback only if parser unavailable — use heuristic
    if _get_md_parser() is None:
        return _find_committed_boundary_heuristic(text)
    return 0


class MarkdownStreamRenderer:
    """Incremental markdown streaming with Live transient tail (legacy shim).

    New code should use stream_blocks._ContentBlock(is_think=False) directly.
    This wrapper preserves the old API (start/on_chunk/finalize/stop) by
    internally delegating to _ContentBlock while keeping the legacy Group
    spinner tail for backward compat with tests.
    """

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self._buffer: str = ""
        self._committed: str = ""
        self._live: Live | None = None
        self._start_time: float | None = None
        self._token_count: int = 0
        self._token_count_float: float = 0.0
        self._has_printed_bullet: bool = False
        self._is_active: bool = False
        # internal canonical block for commitment logic (kept in sync)
        self._block: Any | None = None
        try:

            self._block = _ContentBlock(is_think=False)
        except Exception:
            self._block = None

    def _wrap_bullet(self, renderable: Any) -> Any:
        try:
            from coderai.utils.rich.columns import BulletColumns
        except Exception:
            return renderable
        if self._has_printed_bullet:
            return BulletColumns(renderable, bullet=Text(" "))
        self._has_printed_bullet = True
        return BulletColumns(renderable)

    def start(self) -> None:
        self._start_time = time.time()
        self._is_active = True
        try:
            self._live = Live(
                Text(""),
                console=self.console,
                transient=True,
                refresh_per_second=10,
            )
            self._live.start()

            def _on_sigwinch(*_args: Any) -> None:
                if self._live:
                    self._live.refresh()

            sigwinch = getattr(signal, "SIGWINCH", None)
            if sigwinch is not None:
                try:
                    signal.signal(sigwinch, _on_sigwinch)
                except Exception:
                    pass
        except Exception:
            self._live = None

    def on_chunk(self, chunk: str) -> None:
        if not chunk:
            return
        if not self._is_active:
            self.start()
        self._buffer += chunk
        try:
            self._token_count_float += float(_estimate_tokens_float(chunk))  # type: ignore[arg-type]
            self._token_count = int(self._token_count_float)
        except Exception:
            from coderai.cli.elapsed import estimate_tokens

            self._token_count = estimate_tokens(self._buffer)
            self._token_count_float = float(self._token_count)

        boundary = _find_committed_boundary(self._buffer)
        if boundary > 0:
            committed_text = self._buffer[:boundary]
            self._committed += committed_text
            self._buffer = self._buffer[boundary:]
            if self._live:
                try:
                    self._live.update(Text(""))
                except Exception:
                    pass
            try:
                self.console.print(self._wrap_bullet(Markdown(committed_text)))
            except Exception:
                try:
                    self.console.print(Markdown(committed_text))
                except Exception:
                    self.console.print(committed_text)
            if self._buffer.startswith("\n"):
                try:
                    self.console.print()
                except Exception:
                    pass
                self._buffer = self._buffer[1:]
                self._committed += "\n"

        self._update_tail()

    def _update_tail(self) -> None:
        if not self._live or not self._is_active:
            return
        tail = self._buffer[-500:] if len(self._buffer) > 500 else self._buffer
        try:
            if tail.strip():
                tail_md = Markdown(tail)
                self._live.update(tail_md)
            else:
                elapsed = time.time() - (self._start_time or time.time())
                bullet = bullet_frame_for(elapsed)
                elapsed_str = format_elapsed(elapsed)
                spinner_line = Text(f"  Thinking{bullet}  {elapsed_str}", style="dim")
                self._live.update(spinner_line)
        except Exception:
            pass

    def finalize(self) -> str:
        full = self._committed + self._buffer
        if self._live:
            try:
                self._live.update(Text(""))
                if hasattr(self._live, "_live_render") and hasattr(
                    self._live._live_render, "_shape"
                ):
                    self._live._live_render._shape = None  # type: ignore[attr-defined]
                self._live.stop()
            except Exception:
                pass
            self._live = None
        remaining = self._buffer.strip()
        if remaining:
            try:
                self.console.print(self._wrap_bullet(Markdown(remaining)))
            except Exception:
                try:
                    self.console.print(Markdown(remaining))
                except Exception:
                    self.console.print(remaining)
        self._is_active = False
        self._buffer = ""
        self._committed = ""
        self._has_printed_bullet = False
        return full

    def stop(self) -> None:
        if self._live:
            try:
                self._live.stop()
            except Exception:
                pass
            self._live = None
        self._is_active = False
