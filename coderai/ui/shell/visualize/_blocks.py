"""Stream blocks and visual rendering for interactive turns."""

from __future__ import annotations

import re
import shutil
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.markup import escape
from rich.spinner import Spinner
from rich.style import Style
from rich.text import Text

from coderai.cli.elapsed import (
    _estimate_tokens_float as _estimate_tokens,
)
from coderai.cli.elapsed import (
    bullet_frame_for,
    format_context_status,
    format_elapsed,
)
from coderai.cli.elapsed import (
    format_token_count_compact as format_token_count,
)
from coderai.ui.shell.console import console
from coderai.utils.rich.columns import BulletColumns
from coderai.utils.rich.markdown import Markdown

from ._markdown_boundary import (
    find_committed_boundary as _find_committed_boundary,
)
from ._markdown_stream import MarkdownStreamRenderer as _MarkdownStreamRenderer
from ._todos import (
    TodoItem as TodoItem,
)
from ._todos import (
    create_todo_block as create_todo_block,
)
from ._todos import (
    format_plan_content as format_plan_content,
)
from ._todos import (
    format_todo_content as format_todo_content,
)
from ._todos import (
    format_todo_item as format_todo_item,
)
from ._todos import (
    parse_plan_stats as parse_plan_stats,
)
from ._todos import (
    render_plan_preview as render_plan_preview,
)
from ._todos import (
    render_todo_list as render_todo_list,
)
from ._tool_cards import (
    _render_collapsible_block as _render_collapsible_block,
)
from ._tool_cards import (
    _render_search_card as _render_search_card,
)
from ._tool_cards import (
    parse_tool_message as parse_tool_message,
)
from ._tool_cards import (
    render_tool_card as render_tool_card,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_ELLIPSIS = "..."
_THINKING_PREVIEW_LINES = 6

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


# ---------------------------------------------------------------------------
# _ContentBlock — core streaming block
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
            console.print(self._wrap_bullet(Markdown(committed_text)))
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

    def _compose_labeled_spinner(self, label: str) -> Spinner:
        elapsed = time.monotonic() - self._start_time
        elapsed_str = format_elapsed(elapsed)
        count_str = f"{format_token_count(int(self._token_count))} tokens"
        self._spinner.text = Text.assemble(
            (label, ""),
            (f" {elapsed_str}", "grey50"),
            (f" · {count_str}", "grey50"),
        )
        return self._spinner

    def _compose_spinner(self) -> Spinner:
        return self._compose_labeled_spinner("Composing...")

    def _compose_thinking_stream(self) -> RenderableType:
        spinner = self._compose_thinking_spinner()
        pending = self._pending_text()
        if not pending:
            return spinner
        preview = self._build_preview(pending)
        return Group(spinner, Text(preview, style="grey50 italic"))

    def _compose_thinking_spinner(self) -> Spinner:
        return self._compose_labeled_spinner("Thinking...")

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


@dataclass
class StatusUpdate:
    context_usage: float | None = None
    context_tokens: int | None = None
    max_context_tokens: int | None = None


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


# Helpers exposed for testing / Live view
def _format_step_retry(retry: Any) -> Text:
    """Minimal StepRetry banner."""
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

    Unified path uses _ContentBlock(is_think=True) with
    Live(Group, transient). This shim is retained for test compat and
    non-TTY fallback; it delegates token math to elapsed but keeps the
    raw ANSI line for width-bound checks.
    """

    SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, console: Any | None = None) -> None:
        self.console = console
        self.thinking_chunks: list[str] = []
        self.start_time: float | None = None
        self.is_active: bool = False
        self.frame_idx: int = 0
        self._last_render_time: float = 0.0
        self._last_line_len: int = 0

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
        try:
            from rich.cells import cell_len as _cell_len
        except Exception:
            _cell_len = len  # type: ignore[assignment]
        if _cell_len(visible) > term_width:
            visible = _truncate_to_display_width(visible, term_width)
            line = f"\r\x1b[2K{visible}"
        else:
            line = (
                f"\r\x1b[2K  \x1b[35m\x1b[1m{frame}\x1b[0m"
                f" \x1b[1;35mReasoning{bullet}\x1b[0m"
                f" \x1b[36m{elapsed_str}\x1b[0m"
            )
        # Pad to overwrite any previously rendered longer line.
        # All lengths are display-cell widths so CJK frames stay aligned.
        pad = max(0, self._last_line_len - _cell_len(visible))
        if pad:
            line += " " * pad + f"\x1b[{pad}D"
        sys.stdout.write(line)
        sys.stdout.flush()
        self._last_line_len = _cell_len(visible)

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


def _install_sigwinch(handler: Callable[..., None]) -> None:
    # NOTE: chain (don't clobber) the previous SIGWINCH handler — Rich Live,
    # prompt_toolkit, and other spinners each install their own, and a plain
    # signal.signal() overwrite breaks resize reflow for everyone else.
    try:
        previous = signal.getsignal(getattr(signal, "SIGWINCH", 0))

        def _wrapped(*args: Any) -> None:
            try:
                handler(*args)
            except Exception:
                pass
            try:
                if callable(previous):
                    previous(*args)
            except Exception:
                pass

        sigwinch = getattr(signal, "SIGWINCH", None)
        if sigwinch is not None:
            signal.signal(sigwinch, _wrapped)
    except Exception:
        pass


def _reset_live_shape(live: Live | None) -> None:
    """Clear cached Live height so next refresh re-anchors after pager or resize."""
    if live is None:
        return
    try:
        if hasattr(live, "_live_render") and hasattr(live._live_render, "_shape"):  # type: ignore[attr-defined]
            live._live_render._shape = None  # type: ignore[attr-defined]
    except Exception:
        pass


class MarkdownStreamRenderer(_MarkdownStreamRenderer):
    """Stable fallback entry point with the shell's resize handler."""

    def __init__(self, console: Console | None = None) -> None:
        super().__init__(console, install_sigwinch=_install_sigwinch)
