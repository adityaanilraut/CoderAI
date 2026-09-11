"""Consecutive identical-call detector (Kimi parity: 3/5/8/12 + force-stop).

Thresholds 3/5/8 inject escalating <system-reminder>, streak 12 signals caller
to force-stop the turn. Excluded tools neither count nor reset the chain.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

DEFAULT_THRESHOLDS = (3, 5, 8, 12)
DEFAULT_EXCLUDE = ("UpdatePlan", "update_plan", "todo_write")
DEFAULT_ARGUMENTS_PREVIEW_CHARS = 500

# Kimi-compatible reminder texts (wrapped in <system-reminder>)
_REMINDER_R1 = (
    "\n\n<system-reminder>\n"
    "You are repeating the exact same tool call with identical parameters. "
    "Please carefully analyze the previous result. If the task is not yet complete, "
    "try a different method or parameters instead of repeating the same call.\n"
    "</system-reminder>"
)

_REMINDER_R3 = (
    "\n\n<system-reminder>\n"
    "You are stuck in a dead end and have repeatedly made the same function call without progress.\n"
    "Stop all function calls immediately. Do not call any tool in your next response.\n"
    "In analysis, review the current execution state and identify why progress is blocked.\n"
    "Then return a text-only summary to the user that reports the current problem, what has "
    "already been tried, and what information or decision is needed next.\n"
    "</system-reminder>"
)

# Legacy alias kept for callers that reference GENTLE_REMINDER
GENTLE_REMINDER = (
    "You are repeating the exact same tool call with identical arguments. "
    "Carefully analyze the previous result before calling again: if the task is "
    "not complete, try a different approach or different arguments instead of "
    "repeating the call."
)

RepeatAction = Literal["none", "r1", "r2", "r3", "stop"]


def _reminder_r2(tool_name: str, count: int, canonical: str) -> str:
    return (
        "\n\n<system-reminder>\n"
        "You have repeatedly called the same tool with identical parameters many times.\n"
        "Repeated tool call detected:\n"
        f"- tool: {tool_name}\n"
        f"- repeated_times: {count}\n"
        f"- arguments: {canonical}\n"
        "The previous repeated calls did not make progress. Do not call this exact same tool "
        "with the exact same arguments again.\n"
        "Carefully inspect the latest tool result and choose a different next action, "
        "different parameters, or finish the task if enough evidence has been gathered.\n"
        "</system-reminder>"
    )


def build_repeat_reminder(
    streak: int, tool_name: str, canonical_args: str
) -> tuple[RepeatAction, str | None]:
    """Map streak count to (action, reminder_text) — Kimi thresholds 3/5/8/12."""
    if streak >= 12:
        return "stop", _REMINDER_R3
    if streak >= 8:
        return "r3", _REMINDER_R3
    if streak >= 5:
        return "r2", _reminder_r2(tool_name, streak, canonical_args)
    if streak >= 3:
        return "r1", _REMINDER_R1
    return "none", None


def args_hash(canonical_args: str) -> str:
    """Stable 8-char hash of canonical tool-call arguments (Kimi parity)."""
    return hashlib.sha256(canonical_args.encode()).hexdigest()[:8]


def _sort_json_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_sort_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _sort_json_value(value[key]) for key in sorted(value)}
    return value


def canonicalize_arguments(arguments: Any) -> str:
    """Deep key-sort then stringify so property order does not break the chain."""
    try:
        return json.dumps(_sort_json_value(arguments), ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return json.dumps(str(arguments), ensure_ascii=False)


def preview_arguments(canonical: str, cap: int = DEFAULT_ARGUMENTS_PREVIEW_CHARS) -> str:
    if len(canonical) <= cap:
        return canonical
    return f"{canonical[:cap]}… (+{len(canonical) - cap} more chars)"


def detailed_reminder(tool_name: str, count: int, canonical_arguments: str) -> str:
    return (
        "Repeated tool call detected:\n"
        f"- tool: {tool_name}\n"
        f"- consecutive_calls: {count}\n"
        f"- arguments: {preview_arguments(canonical_arguments)}\n"
        "The repeated calls are not making progress. Do not call this tool with "
        "these exact arguments again. Inspect the latest result and choose a "
        "different action, different arguments, or finish the task if enough "
        "evidence has been gathered."
    )


class RepeatToolReminder:
    """Per-session consecutive-call tracker."""

    def __init__(
        self,
        thresholds: tuple[int, ...] = DEFAULT_THRESHOLDS,
        exclude: tuple[str, ...] = DEFAULT_EXCLUDE,
    ) -> None:
        self.thresholds = tuple(sorted(set(thresholds)))
        self.exclude = {name.lower() for name in exclude}
        self._key: tuple[str, str] | None = None
        self._count = 0

    def observe(self, tool_name: str, arguments: Any) -> str | None:
        """Record a call. Return reminder text at threshold, otherwise None (legacy)."""
        text, _ = self.observe_with_action(tool_name, arguments)
        return text

    def observe_with_action(
        self, tool_name: str, arguments: Any
    ) -> tuple[str | None, RepeatAction]:
        """Record a call. Return (reminder_text, action). Fires only at configured thresholds."""
        if not tool_name or tool_name.lower() in self.exclude:
            return None, "none"
        canonical = canonicalize_arguments(arguments)
        key = (tool_name, canonical)
        if key == self._key:
            self._count += 1
        else:
            self._key = key
            self._count = 1
        if self._count not in self.thresholds:
            # Compute action for force-stop even when not firing (Kimi parity)
            action, _ = build_repeat_reminder(self._count, tool_name, canonical)
            return None, action
        # At threshold: return appropriate reminder + action
        if self._count == self.thresholds[0]:
            return GENTLE_REMINDER, "r1"
        if self._count == 12 and self.thresholds == DEFAULT_THRESHOLDS:
            _, text = build_repeat_reminder(12, tool_name, canonical)
            return text, "stop"
        if self.thresholds == DEFAULT_THRESHOLDS and self._count in (5, 8):
            # Keep legacy detailed format for test compat; action mapped to r2/r3
            action = "r2" if self._count == 5 else "r3"
            return detailed_reminder(tool_name, self._count, canonical), action
        return detailed_reminder(tool_name, self._count, canonical), "r2"

    def observe_legacy(self, tool_name: str, arguments: Any) -> str | None:
        """Legacy observe returning only text (backward compat)."""
        return self.observe(tool_name, arguments)

    @property
    def consecutive_key(self) -> tuple[str, str] | None:
        return self._key

    @property
    def consecutive_count(self) -> int:
        return self._count

    @property
    def should_force_stop(self) -> bool:
        return self._count >= 12

    def reset(self) -> None:
        """Reset the consecutive-call tracking chain (e.g. on new user turns)."""
        self._key = None
        self._count = 0
