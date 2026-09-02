"""Centralized terminal color theme definitions.

Provides dark/light switching for diff background colors, syntax theme,
and prompt styling. Pure CLI, no browser layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from rich.style import Style as RichStyle

ThemeName = Literal["dark", "light"]


# ---------------------------------------------------------------------------
# Diff colors
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DiffColors:
    add_bg: RichStyle
    del_bg: RichStyle
    add_hl: RichStyle
    del_hl: RichStyle


_DIFF_DARK = DiffColors(
    add_bg=RichStyle(bgcolor="#12261e"),
    del_bg=RichStyle(bgcolor="#2d1214"),
    add_hl=RichStyle(bgcolor="#1a4a2e"),
    del_hl=RichStyle(bgcolor="#5c1a1d"),
)

_DIFF_LIGHT = DiffColors(
    add_bg=RichStyle(bgcolor="#dafbe1"),
    del_bg=RichStyle(bgcolor="#ffebe9"),
    add_hl=RichStyle(bgcolor="#aff5b4"),
    del_hl=RichStyle(bgcolor="#ffc1c0"),
)


_active_theme: ThemeName = "dark"


def set_active_theme(theme: ThemeName) -> None:
    global _active_theme
    _active_theme = theme


def get_active_theme() -> ThemeName:
    return _active_theme


def get_diff_colors() -> DiffColors:
    return _DIFF_LIGHT if _active_theme == "light" else _DIFF_DARK


# ponytail: truecolor probe is coarse (env sniff), per-console detection if needed later
def supports_truecolor() -> bool:
    """Return True if terminal likely supports 24-bit truecolor; graceful fallback to 256."""
    import os
    import sys

    if os.getenv("NO_COLOR") is not None:
        return False
    colorterm = (os.getenv("COLORTERM") or "").lower()
    if colorterm in ("truecolor", "24bit"):
        return True
    term = (os.getenv("TERM") or "").lower()
    if "truecolor" in term or "24bit" in term:
        return True
    # xterm-256color alone is 256, not truecolor — treat as fallback
    if not sys.stdout.isatty():
        return False
    return False


def get_color_system() -> str | None:
    """Rich color_system hint: 'truecolor' or '256' or None for autodetect."""
    import os

    if os.getenv("NO_COLOR") is not None:
        return None
    return "truecolor" if supports_truecolor() else "256"
