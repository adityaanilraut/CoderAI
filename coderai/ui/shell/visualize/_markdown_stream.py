"""Fallback Markdown streaming with the shared parser and background policy."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.text import Text

from coderai.cli.elapsed import _estimate_tokens_float, bullet_frame_for, format_elapsed
from coderai.utils.rich.markdown import Markdown

from ._markdown_boundary import find_committed_boundary


class MarkdownStreamRenderer:
    """Progressive Markdown renderer used when the unified Live path is unavailable."""

    def __init__(
        self,
        console: Console | None = None,
        *,
        install_sigwinch: Callable[[Callable[..., None]], None] | None = None,
    ) -> None:
        self.console = console or Console()
        self._install_sigwinch = install_sigwinch
        self._buffer: str = ""
        self._committed: str = ""
        self._live: Live | None = None
        self._start_time: float | None = None
        self._token_count: int = 0
        self._token_count_float: float = 0.0
        self._has_printed_bullet: bool = False
        self._is_active: bool = False

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

            # Chain via helper so an existing SIGWINCH handler survives.
            if self._install_sigwinch is not None:
                self._install_sigwinch(_on_sigwinch)
        except Exception:
            self._live = None

    def on_chunk(self, chunk: str) -> None:
        if not chunk:
            return
        if not self._is_active:
            self.start()
        self._buffer += chunk
        try:
            self._token_count_float += _estimate_tokens_float(chunk)
            self._token_count = int(self._token_count_float)
        except Exception:
            from coderai.cli.elapsed import estimate_tokens

            self._token_count = estimate_tokens(self._buffer)
            self._token_count_float = float(self._token_count)

        boundary = find_committed_boundary(self._buffer)
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
