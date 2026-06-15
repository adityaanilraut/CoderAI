"""Platform-aware UI strings for keyboard shortcut hints."""

from __future__ import annotations

import os
import sys


def is_macos() -> bool:
    return sys.platform == "darwin"


def supports_truecolor() -> bool:
    """Best-effort detection of 24-bit (truecolor) terminal support.

    The Tokyo Night palette relies on exact hex colors, so a 256-color terminal
    renders it inaccurately. Apple's Terminal.app caps at 256 colors and cannot
    display 24-bit color even when ``COLORTERM`` is forced to ``truecolor``, so
    it is always treated as non-truecolor regardless of that variable.
    """
    if os.environ.get("TERM_PROGRAM") == "Apple_Terminal":
        return False
    if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return True
    term = os.environ.get("TERM", "").lower()
    return "truecolor" in term or "direct" in term


def truecolor_hint() -> str:
    """Warning shown when the terminal can't render the Tokyo Night palette."""
    return (
        "256-color terminal detected (Apple Terminal?) — the Tokyo Night theme "
        "needs truecolor. Try iTerm2, Ghostty, WezTerm, Kitty, or the VS Code terminal."
    )


def palette_shortcut() -> str:
    return "⌘K" if is_macos() else "Ctrl+K"


def composer_placeholder() -> str:
    return (
        f"Message CoderAI…   / commands   @ mention   ↑ history   "
        f"{palette_shortcut()} palette"
    )


def header_palette_hint() -> str:
    return f"{palette_shortcut()} palette • ⎋ cancel • @ mention • / slash"


def composer_footer_hints() -> str:
    return (
        f"↵ send · ⇧↵ newline · ↑↓ history · @ mention · / commands · {palette_shortcut()} palette"
    )


def palette_input_placeholder() -> str:
    return f"{palette_shortcut()} Type to search commands, models, personas…"


__all__ = [
    "composer_footer_hints",
    "composer_placeholder",
    "header_palette_hint",
    "palette_input_placeholder",
]
