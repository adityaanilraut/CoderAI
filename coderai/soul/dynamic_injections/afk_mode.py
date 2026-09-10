# Moved from coderai/soul/dynamic_injection.py - kimi structure
# (kimi_cli/soul/dynamic_injections/afk_mode.py).
"""Afk (away-from-keyboard) injection provider (Kimi ``afk_mode`` parity).

Re-exported from :mod:`coderai.soul.dynamic_injection` so the old path
keeps working.
"""

from __future__ import annotations

from typing import Any

from coderai.soul.dynamic_injection import (
    DynamicInjection,
    DynamicInjectionProvider,
    SoulView,
)

AFK_INJECTION_TYPE = "afk_mode"

AFK_PROMPT_ROOT = (
    "You are running in afk mode. No user is present to answer "
    "questions or approve actions. All tool calls are auto-approved by "
    "the harness.\n"
    "- Do NOT call AskUserQuestion — it will be auto-dismissed with no "
    "answer, wasting a turn. Make your best judgment and proceed.\n"
    "- Finish the user's request end-to-end in this run. Do not defer "
    "decisions to a human."
)

AFK_DISABLED_REMINDER = (
    "Afk mode is now disabled. The user is back at the terminal and CAN answer "
    "AskUserQuestion.\n"
    "- Ignore any earlier afk mode reminders that said no user is present or "
    "that you must not call AskUserQuestion.\n"
    "- Tool calls are no longer auto-approved by afk. They may still be "
    "auto-approved if yolo mode remains active."
)


class AfkModeInjectionProvider(DynamicInjectionProvider):
    """One-shot afk guidance while no user is present (root sessions only)."""

    def __init__(self) -> None:
        self._injected = False

    async def get_injections(
        self, history: list[dict[str, Any]], soul: SoulView
    ) -> list[DynamicInjection]:
        _ = history
        if not soul.is_afk or soul.is_subagent or self._injected:
            return []
        self._injected = True
        return [DynamicInjection(type=AFK_INJECTION_TYPE, content=AFK_PROMPT_ROOT)]

    async def on_context_compacted(self) -> None:
        self._injected = False

    async def on_afk_changed(self, enabled: bool) -> None:
        _ = enabled
        self._injected = False


__all__ = [
    "AFK_INJECTION_TYPE",
    "AFK_PROMPT_ROOT",
    "AFK_DISABLED_REMINDER",
    "AfkModeInjectionProvider",
]
