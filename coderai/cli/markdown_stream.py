"""Live token & markdown streaming renderer — shim over stream_blocks.

Re-exports canonical streaming helpers and provides MarkdownStreamRenderer
for progressive markdown rendering and formatting.
"""

from __future__ import annotations

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
from coderai.cli.stream_blocks import _get_md_parser

try:
    from coderai.cli.syntax_theme import KIMI_ANSI_THEME  # noqa: F401
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


def _find_committed_boundary_parser(text: str) -> int | None:
    """Delegate to canonical stream_blocks parser."""
    from coderai.cli.stream_blocks import _find_committed_boundary as _sb

    return _sb(text)


def _find_committed_boundary(text: str) -> int:
    """Find index up to which markdown is safe to flush (authoritative None->0)."""
    from coderai.cli.stream_blocks import _find_committed_boundary as _sb

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
            from coderai.cli.stream_blocks import _ContentBlock

            self._block = _ContentBlock(is_think=False)
        except Exception:
            self._block = None

    def _wrap_bullet(self, renderable: Any) -> Any:
        try:
            from coderai.cli.columns import BulletColumns
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
