"""Neutral console, pager, and OSC-8 helpers — Phase0 port of Kimi ui/shell/console.py.

Provides:
- NEUTRAL_MARKDOWN_THEME that disables noisy markdown colors
- _KimiPager that strips MANPAGER to avoid `col|bat` mangling
- _KimiConsole that defaults to _KimiPager
- render_to_ansi with OSC-8 zero-width wrapping for prompt_toolkit
"""

from __future__ import annotations

import os
import pydoc
import re

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


class _KimiPager(Pager):
    """Pager that ignores MANPAGER to avoid garbled ANSI output."""

    def show(self, content: str) -> None:
        saved = os.environ.pop("MANPAGER", None)
        try:
            pydoc.pager(content)
        finally:
            if saved is not None:
                os.environ["MANPAGER"] = saved


class _KimiConsole(Console):
    """Console subclass that defaults to :class:`_KimiPager`."""

    def pager(
        self,
        pager: Pager | None = None,
        styles: bool = False,
        links: bool = False,
    ) -> PagerContext:
        if pager is None:
            pager = _KimiPager()
        return super().pager(pager=pager, styles=styles, links=links)


# Global console — use this everywhere (Kimi parity: highlight=False)
console = _KimiConsole(highlight=False, theme=NEUTRAL_MARKDOWN_THEME)

# Matches OSC 8 hyperlink open/close markers: ESC ] 8 ; params ; uri ST (ST = ESC \ or BEL)
_OSC8_RE = re.compile(r"\x1b\]8;[^\x07\x1b]*(?:\x1b\\|\x07)")


def _wrap_osc8_as_zero_width(m: re.Match[str]) -> str:
    return f"\x01{m.group(0)}\x02"


def render_to_ansi(renderable: RenderableType, *, columns: int) -> str:
    """Render a Rich renderable to ANSI for prompt_toolkit (wraps OSC-8 as ZeroWidthEscape)."""
    from io import StringIO

    width = max(20, columns)
    buf = StringIO()
    temp = Console(
        file=buf,
        force_terminal=True,
        width=width,
        theme=NEUTRAL_MARKDOWN_THEME,
        highlight=False,
    )
    temp.print(renderable, end="")
    result = buf.getvalue()
    return _OSC8_RE.sub(_wrap_osc8_as_zero_width, result)
