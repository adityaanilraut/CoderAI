"""Thinking mode visual hierarchy and reasoning styling.

Canonical reasoning rendering lives here; live streaming pulse is
now unified in coderai.cli.stream_blocks._ContentBlock(is_think=True).
This module keeps the stable public API (summarize_thinking,
render_thinking_block) and retains LiveThinkingStreamer as legacy
fallback for non-Rich / non-TTY paths (raw \\r). New live code should
use stream_blocks.
"""

from __future__ import annotations

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
        term_width = 80
        if (
            active_console is not None
            and isinstance(getattr(active_console, "width", None), int)
            and active_console.width > 0
        ):
            term_width = active_console.width
        else:
            term_width = shutil.get_terminal_size(fallback=(80, 24)).columns

        elapsed_len = len(f"({elapsed_str})") if elapsed_str else 0
        prefix_len = 16 + (elapsed_len + 1 if elapsed_len else 0)
        max_summary_chars = min(140, max(20, term_width - prefix_len - 2))

        summary = summarize_thinking(thinking_text, max_chars=max_summary_chars)
        active_console.print(
            f"  [bold magenta]● Reasoning[/]{duration_str}{tok_str} [dim]•[/] [dim italic]{escape(summary)}[/]"
        )


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
            from coderai.cli.stream_blocks import _ContentBlock

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
        elapsed = time.time() - self.start_time
        full_text = "".join(self.thinking_chunks)
        frame = self.SPINNER_FRAMES[self.frame_idx % len(self.SPINNER_FRAMES)]
        self.frame_idx += 1
        bullet = bullet_frame_for(elapsed)
        elapsed_fmt = format_elapsed(elapsed)
        from coderai.cli.elapsed import estimate_tokens

        tok_count = estimate_tokens(full_text)
        rate = f" · {int(tok_count / elapsed)} tok/s" if elapsed > 0.5 and tok_count else ""
        tok_info = f" · {tok_count} tokens{rate}" if tok_count else ""
        term_width = max(30, self._get_term_width())
        elapsed_str = f"({elapsed_fmt})"
        prefix_len = 17 + len(elapsed_str)
        max_summary_len = max(10, term_width - prefix_len - 2)
        summary = summarize_thinking(full_text, max_chars=max_summary_len)
        line = f"\r\x1b[2K  \x1b[35m\x1b[1m{frame}\x1b[0m \x1b[1;35mReasoning{bullet}\x1b[0m \x1b[36m{elapsed_str}\x1b[0m\x1b[2m{tok_info} • {summary}\x1b[0m"
        sys.stdout.write(line)
        sys.stdout.flush()
        self._last_line_len = prefix_len + len(summary)

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
                from coderai.cli.stream_blocks import _ContentBlock

                self._block = _ContentBlock(is_think=True)
            except Exception:
                pass
