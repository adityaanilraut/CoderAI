"""Neutral console, pager, and OSC-8 helpers.

Provides:
- NEUTRAL_MARKDOWN_THEME that disables noisy markdown colors
- _CoderAIPager that strips MANPAGER to avoid `col|bat` mangling
- _CoderAIConsole that defaults to _CoderAIPager
- render_to_ansi with OSC-8 zero-width wrapping for prompt_toolkit
"""

from __future__ import annotations

import os
import pydoc
import re
import sys
from typing import Any

from rich.console import Console, PagerContext, RenderableType
from rich.pager import Pager
from rich.theme import Theme

NEUTRAL_MARKDOWN_THEME = Theme(
    {
        "markdown.paragraph": "none",
        "markdown.block_quote": "dim italic",
        "markdown.hr": "dim",
        "markdown.list": "none",
        "markdown.item": "none",
        "markdown.item.bullet": "cyan",
        "markdown.item.number": "cyan",
        "markdown.link": "bright_blue underline",
        "markdown.link_url": "cyan underline",
        "markdown.h1": "bold cyan",
        "markdown.h1.border": "none",
        "markdown.h2": "bold cyan",
        "markdown.h3": "bold yellow",
        "markdown.h4": "bold",
        "markdown.h5": "bold",
        "markdown.h6": "bold",
        "markdown.h7": "bold",
        "markdown.em": "italic",
        "markdown.emph": "italic",
        "markdown.strong": "bold",
        "markdown.s": "strike",
        "markdown.code": "bold cyan",
        "markdown.code_block": "none",
        "status.spinner": "none",
    },
    inherit=True,
)

_NEUTRAL_MARKDOWN_THEME = NEUTRAL_MARKDOWN_THEME


class _CoderAIPager(Pager):
    """Pager that ignores MANPAGER to avoid garbled ANSI output."""

    def show(self, content: str) -> None:
        saved = os.environ.pop("MANPAGER", None)
        try:
            pydoc.pager(content)
        finally:
            if saved is not None:
                os.environ["MANPAGER"] = saved


class _CoderAIConsole(Console):
    """Console subclass that defaults to :class:`_CoderAIPager`."""

    def print(self, *objects: Any, **kwargs: Any) -> None:
        from coderai.ui.theme import get_shell_rich_theme

        with self.use_theme(get_shell_rich_theme()):
            super().print(*objects, **kwargs)

    def pager(
        self,
        pager: Pager | None = None,
        styles: bool = False,
        links: bool = False,
    ) -> PagerContext:
        if pager is None:
            pager = _CoderAIPager()
        return super().pager(pager=pager, styles=styles, links=links)


# Unified panel chrome: every informational shell panel uses this border
# and padding so help, picker, welcome, and status cards look like one UI.
# Approval stays yellow and errors stay red; those are state, not chrome.
PANEL_BORDER_STYLE = "cyan"
PANEL_PADDING = (0, 1)


def kv_table(rows: list[tuple[str, Any]]) -> Any:
    """Two-column key/value grid whose label column fits every key.

    Fixed label widths used to clip longer keys ("Configured MCP Servers:",
    "Active Working Context:") and shove the values out of alignment.
    """
    from rich.table import Table

    width = max((len(str(key)) for key, _ in rows), default=0)
    table = Table.grid(expand=True, padding=(0, 2))
    table.add_column(style="dim cyan", width=width, no_wrap=True)
    table.add_column(ratio=1, overflow="fold")
    for key, value in rows:
        table.add_row(str(key), value)
    return table


def no_color_enabled() -> bool:
    """True when output must be plain text (NO_COLOR set or stdout is a pipe)."""
    if os.getenv("NO_COLOR") is not None:
        return True
    try:
        return not sys.stdout.isatty()
    except Exception:
        return False


# Global console — use this everywhere.
# no_color strips markup styles when NO_COLOR is set; Rich already drops
# color on pipes, and the explicit flag covers redirected-but-tty edge cases.
console = _CoderAIConsole(
    highlight=False,
    theme=NEUTRAL_MARKDOWN_THEME,
    no_color=os.getenv("NO_COLOR") is not None,
)

# Matches OSC 8 hyperlink open/close markers: ESC ] 8 ; params ; uri ST (ST = ESC \ or BEL)
_OSC8_RE = re.compile(r"\x1b\]8;[^\x07\x1b]*(?:\x1b\\|\x07)")


def _wrap_osc8_as_zero_width(m: re.Match[str]) -> str:
    return f"\x01{m.group(0)}\x02"


def render_to_ansi(renderable: RenderableType, *, columns: int) -> str:
    """Render a Rich renderable to ANSI for prompt_toolkit (wraps OSC-8 as ZeroWidthEscape)."""
    from io import StringIO

    width = max(20, columns)
    buf = StringIO()
    plain = no_color_enabled()
    temp = Console(
        file=buf,
        force_terminal=not plain,
        width=width,
        theme=NEUTRAL_MARKDOWN_THEME,
        highlight=False,
        no_color=plain,
    )
    temp.print(renderable, end="")
    result = buf.getvalue()
    return _OSC8_RE.sub(_wrap_osc8_as_zero_width, result)
