"""Thinking mode visual hierarchy, live streaming visualizer, and reasoning styling."""

from __future__ import annotations

import re
import sys
import time
from typing import Any

try:
    from rich.panel import Panel
    from rich.text import Text

    _RICH = True
except ImportError:  # pragma: no cover
    Panel = None  # type: ignore[assignment,misc]
    Text = None  # type: ignore[assignment,misc]
    _RICH = False


def summarize_thinking(thinking_text: str, max_chars: int = 140) -> str:
    """Extract a concise one-line summary from a reasoning block."""
    cleaned = re.sub(r"\s+", " ", thinking_text).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 3] + "..."


def render_thinking_block(
    console: Any | None,
    thinking_text: str,
    elapsed_seconds: float | None = None,
    expanded: bool = False,
) -> None:
    """Render a clean, compact thinking mode block or expanded reasoning trace."""
    if not thinking_text.strip():
        return

    duration_str = f" [dim cyan]({elapsed_seconds:.1f}s)[/]" if elapsed_seconds is not None else ""

    if console is not None and _RICH:
        if expanded:
            console.print(f"  [bold magenta]● Reasoning Trace[/]{duration_str}")
            for line in thinking_text.strip().splitlines()[:25]:
                console.print(f"    [dim italic]{line}[/]")
            if len(thinking_text.strip().splitlines()) > 25:
                console.print(f"    [dim]... ({len(thinking_text.strip().splitlines()) - 25} more lines truncated)[/]")
        else:
            summary = summarize_thinking(thinking_text)
            console.print(
                f"  [bold magenta]● Reasoning[/]{duration_str} [dim]•[/] [dim italic]{summary}[/]"
            )
    else:
        summary = summarize_thinking(thinking_text)
        duration_plain = f" ({elapsed_seconds:.1f}s)" if elapsed_seconds is not None else ""
        print(f"  ● Reasoning: {summary}{duration_plain}")



class LiveThinkingStreamer:
    """Live visualizer for streaming reasoning tokens with animated elapsed time."""

    SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, console: Any | None = None) -> None:
        self.console = console
        self.thinking_chunks: list[str] = []
        self.start_time: float | None = None
        self.is_active: bool = False
        self.frame_idx: int = 0
        self._last_render_time: float = 0.0
        self._last_line_len: int = 0

    def on_chunk(self, chunk: str) -> None:
        """Handle incoming thinking chunk."""
        if not chunk:
            return
        if self.start_time is None:
            self.start_time = time.time()
            self.is_active = True

        self.thinking_chunks.append(chunk)
        now = time.time()
        # Throttle terminal redraws to ~15fps (every 0.06s)
        if now - self._last_render_time > 0.06:
            self._render_inline()
            self._last_render_time = now

    def _render_inline(self) -> None:
        """Render active inline thinking status line with spinner and elapsed time."""
        if not self.is_active or not self.start_time:
            return
        elapsed = time.time() - self.start_time
        full_text = "".join(self.thinking_chunks)
        summary = summarize_thinking(full_text, max_chars=80)
        frame = self.SPINNER_FRAMES[self.frame_idx % len(self.SPINNER_FRAMES)]
        self.frame_idx += 1

        if sys.stdout.isatty():
            line = f"\r\x1b[K  \x1b[35m\x1b[1m{frame}\x1b[0m \x1b[1;35mReasoning\x1b[0m \x1b[36m({elapsed:.1f}s)\x1b[0m \x1b[2m• {summary}\x1b[0m"
            sys.stdout.write(line)
            sys.stdout.flush()
            self._last_line_len = len(summary) + 30

    def finalize(self, console: Any | None = None, expanded: bool = False) -> str:
        """Complete live thinking stream, clear inline line, and render final block."""
        if not self.is_active or not self.thinking_chunks:
            self.reset()
            return ""

        elapsed = (time.time() - self.start_time) if self.start_time else None
        full_thinking = "".join(self.thinking_chunks).strip()

        # Clear active inline carriage-return line if in a TTY
        if sys.stdout.isatty() and self.is_active:
            sys.stdout.write("\r\x1b[K")
            sys.stdout.flush()

        active_console = console or self.console
        render_thinking_block(active_console, full_thinking, elapsed_seconds=elapsed, expanded=expanded)

        self.reset()
        return full_thinking

    def reset(self) -> None:
        """Reset internal buffer and timer state."""
        self.thinking_chunks.clear()
        self.start_time = None
        self.is_active = False
        self.frame_idx = 0
        self._last_render_time = 0.0
        self._last_line_len = 0
