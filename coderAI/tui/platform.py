"""Platform-aware UI strings for keyboard shortcut hints."""

from __future__ import annotations

import sys


def is_macos() -> bool:
    return sys.platform == "darwin"


def palette_shortcut() -> str:
    return "⌘K" if is_macos() else "Ctrl+K"


def composer_placeholder() -> str:
    return f"Message CoderAI…   / commands   @ mention   ↑ history   {palette_shortcut()} palette"


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
