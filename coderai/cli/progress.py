"""Progress & status indicators — ported from Kimi CLI visualize/_blocks.py + utils/rich.

Provides:
- Dynamic spinners (dots/moon/balloon) with elapsed ticker
- Multi-step progress bar
- Live status badges (Thinking..., Searching..., Executing...)
Pure CLI.
"""

from __future__ import annotations

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

        signal.signal(signal.SIGWINCH, _wrapped)  # type: ignore[arg-type]
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
