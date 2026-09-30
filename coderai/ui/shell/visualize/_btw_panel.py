"""BTW side question modal — Phase4 lean port of upstream _btw_panel.py:31-224.

Q: question bold cyan, ─ sep, Spinner dots yellow+Markdown, auto-scroll
tail 20 lines, ↑ X above·↓ Y below bordered hint, modal_priority=5.
ponytail: no border-ANSI extraction dance; uses Panel + Group tail.
"""

from __future__ import annotations

import re
import time
from typing import Any
from collections.abc import Callable

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text

from coderai.ui.shell.console import render_to_ansi
from coderai.cli.elapsed import format_elapsed
from coderai.utils.rich.markdown import Markdown


_LEFT_BORDER_RE = re.compile(r"((?:\x1b\[[^m]*m)*│(?:\x1b\[[^m]*m)* )")
_RIGHT_BORDER_RE = re.compile(r"( (?:\x1b\[[^m]*m)*│(?:\x1b\[[^m]*m)*)$")

_BTW_MAX_VISIBLE_LINES = 20
_BTW_SHORT_ANSWER_LINES = 3


def _truncate_cells(text: str, max_cells: int) -> str:
    """Truncate to a display-cell budget (CJK-safe); pad with spaces."""
    try:
        from rich.cells import cell_len
    except Exception:
        return text.ljust(max_cells)[:max_cells]
    width = 0
    chars: list[str] = []
    for ch in text:
        w = cell_len(ch)
        if width + w > max_cells:
            break
        chars.append(ch)
        width += w
    return "".join(chars) + " " * max(0, max_cells - width)


def _build_bordered_line(text: str, reference_line: str, columns: int) -> str:
    import os as _os
    import sys as _sys

    inner = max(0, columns - 4)
    fitted = _truncate_cells(text, inner)
    try:
        piped = not _sys.stdout.isatty()
    except Exception:
        piped = True
    if _os.getenv("NO_COLOR") is not None or piped:
        return f"  {fitted.strip()}"
    left_m = _LEFT_BORDER_RE.match(reference_line)
    right_m = _RIGHT_BORDER_RE.search(reference_line)
    if not left_m or not right_m:
        return f"  {fitted.strip()}"
    left = left_m.group(1)
    right = right_m.group(1)
    dim = "\x1b[2m"
    reset = "\x1b[0m"
    return f"{left}{dim}{fitted}{reset}{right}"


class BtwPanel:
    """BTW modal delegate — replaces prompt line with Q/A panel."""

    modal_priority = 5

    def __init__(self, *, on_dismiss: Callable[[], None] | None = None) -> None:
        self._on_dismiss = on_dismiss or (lambda: None)
        self._question: str = ""
        self._response: str | None = None
        self._error: str | None = None
        self._is_loading: bool = True
        self._spinner: Spinner = Spinner("dots", style="yellow")
        self._streaming_text: str = ""
        self._scroll_offset: int = 0
        self._auto_scroll: bool = True
        self._start_time: float = 0.0

    @property
    def question(self) -> str:
        return self._question

    def set_question(self, q: str) -> None:
        self._question = q

    def set_start_time(self, t: float) -> None:
        self._start_time = t

    def append_text(self, chunk: str) -> None:
        self._streaming_text += chunk

    def set_result(self, response: str | None, error: str | None) -> None:
        self._response = response
        self._error = error
        self._is_loading = False
        self._scroll_offset = 0
        self._auto_scroll = False

    def _build_title(self) -> str:
        if self._is_loading:
            elapsed = time.monotonic() - self._start_time if self._start_time else 0.0
            elapsed_str = format_elapsed(elapsed)
            cc = len(self._streaming_text)
            if cc > 0:
                return f"[bold]btw[/bold] [dim]· answering {elapsed_str} · {cc} chars[/dim]"
            return f"[bold]btw[/bold] [dim]· answering {elapsed_str}[/dim]"
        if self._error:
            return "[bold]btw[/bold] [dim]· error[/dim]"
        return "[bold]btw[/bold]"

    def render(self, columns: int = 80) -> Panel:
        return self._build_panel(columns)

    def _build_panel(self, columns: int) -> Panel:
        parts: list[RenderableType] = []
        q_text = Text()
        q_text.append("Q: ", style="bold cyan")
        q_text.append(self._question)
        parts.append(q_text)
        # Panel chrome is 2 border cells + 2 padding cells, so the rule
        # matches the inner content width instead of stopping short of it.
        parts.append(Text("─" * max(1, columns - 4), style="grey50"))
        if self._is_loading:
            if self._streaming_text:
                if Markdown is not None:
                    try:
                        parts.append(Markdown(self._streaming_text))
                    except Exception:
                        parts.append(Text(self._streaming_text))
                else:
                    parts.append(Text(self._streaming_text))
                parts.append(Text("Composing...", style="yellow italic"))
            else:
                parts.append(Spinner("dots", text=Text("Thinking...", style="yellow italic")))
        elif self._error:
            parts.append(Text(self._error, style="red"))
            parts.append(Text(""))
        # Answer (loading and error states are fully rendered above;
        # this also avoids re-adding "No response received." after an error).
        if self._is_loading or self._error:
            pass
        elif self._response:
            try:
                parts.append(Markdown(self._response))
            except Exception:
                parts.append(Text(self._response))
            parts.append(Text(""))
            parts.append(Text("↑/↓ scroll · Escape dismiss", style="dim"))
        else:
            parts.append(Text("No response received.", style="dim"))
            parts.append(Text(""))
            parts.append(Text("Escape to dismiss", style="dim"))
        return Panel(
            Group(*parts),
            title=self._build_title(),
            title_align="left",
            border_style="cyan",
            padding=(0, 1),
        )

    def compose_for_live(self, columns: int = 80) -> RenderableType:
        panel = self._build_panel(columns)
        # Quick path: if total lines small, return panel
        try:
            full = render_to_ansi(panel, columns=columns).rstrip("\n")
            lines = full.split("\n")
            total = len(lines)
            if total <= _BTW_MAX_VISIBLE_LINES:
                return panel
            # Scroll mode — slice content, keep borders, replace last visible with hint
            content = lines[1:-1]
            if self._auto_scroll:
                max_content = _BTW_MAX_VISIBLE_LINES - 2
                self._scroll_offset = max(0, len(content) - max_content)
            max_content = _BTW_MAX_VISIBLE_LINES - 2
            max_offset = max(0, len(content) - max_content)
            self._scroll_offset = min(self._scroll_offset, max_offset)
            start = self._scroll_offset
            visible = content[start : start + max_content]
            above = start
            below = max_offset - start
            hint_parts: list[str] = []
            if above > 0:
                hint_parts.append(f"↑ {above} above")
            if below > 0:
                hint_parts.append(f"↓ {below} below")
            hint_parts.append("↑/↓ scroll · Escape dismiss")
            hint_text = "  ·  ".join(hint_parts)
            hint_line = _build_bordered_line(hint_text, content[0] if content else "", columns)
            if visible:
                visible[-1] = hint_line
            # Return ANSI string joined — for Live we return Text with ANSI

            # ponytail: return ANSI via Text.from_ansi fallback — simplest is return Panel tail via Text
            # For now return panel (scroll hint shown via dim line inside) — ANSI slicing edge case skipped if not terminal
            return panel
        except Exception:
            return panel

    # PTK delegate protocol stubs
    def running_prompt_placeholder(self) -> str | None:
        return None

    def running_prompt_allows_text_input(self) -> bool:
        return False

    def running_prompt_hides_input_buffer(self) -> bool:
        return True

    def running_prompt_accepts_submission(self) -> bool:
        return False

    def should_handle_running_prompt_key(self, key: str) -> bool:
        if self._is_loading:
            return key in {"escape", "c-c", "c-d", "up", "down"}
        return key in {"escape", "enter", "space", "c-c", "c-d", "up", "down"}

    def handle_running_prompt_key(self, key: str, event: Any) -> None:
        if key in {"up", "down"}:
            self._auto_scroll = False
            if key == "up":
                self._scroll_offset = max(0, self._scroll_offset - 3)
            else:
                self._scroll_offset += 3
            return
        self._on_dismiss()

    # Alias for _LiveView compose
    def render_running_prompt_body(self, columns: int) -> Any:
        from prompt_toolkit.formatted_text import ANSI  # type: ignore

        panel = self._build_panel(columns)
        full = render_to_ansi(panel, columns=columns).rstrip("\n")
        lines = full.split("\n")
        total = len(lines)
        if total <= _BTW_MAX_VISIBLE_LINES:
            return ANSI("\n".join(lines))
        # scroll mode
        border_top = lines[0]
        border_bottom = lines[-1]
        content = lines[1:-1]
        if self._auto_scroll:
            max_content = _BTW_MAX_VISIBLE_LINES - 2
            self._scroll_offset = max(0, len(content) - max_content)
        max_content = _BTW_MAX_VISIBLE_LINES - 2
        max_offset = max(0, len(content) - max_content)
        self._scroll_offset = min(self._scroll_offset, max_offset)
        start = self._scroll_offset
        visible = content[start : start + max_content]
        above = start
        below = max_offset - start
        hint_parts: list[str] = []
        if above > 0:
            hint_parts.append(f"↑ {above} above")
        if below > 0:
            hint_parts.append(f"↓ {below} below")
        hint_parts.append("↑/↓ scroll · Escape dismiss")
        hint_text = "  ·  ".join(hint_parts)
        hint_line = _build_bordered_line(hint_text, content[0] if content else "", columns)
        if visible:
            visible[-1] = hint_line
        result = [border_top, *visible, border_bottom]
        return ANSI("\n".join(result))


# Backward compat alias
_BtwModalDelegate = BtwPanel
