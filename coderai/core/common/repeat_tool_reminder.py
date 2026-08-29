"""Advisory consecutive identical-call detector (dsh-repeat-tool-reminder).

Never blocks execution. Thresholds 3 / 5 / 8. Excluded tools neither count
nor reset the chain.
"""

from __future__ import annotations

import json
from typing import Any

DEFAULT_THRESHOLDS = (3, 5, 8)
DEFAULT_EXCLUDE = ("UpdatePlan", "update_plan", "todo_write")
DEFAULT_ARGUMENTS_PREVIEW_CHARS = 500

GENTLE_REMINDER = (
    "You are repeating the exact same tool call with identical arguments. "
    "Carefully analyze the previous result before calling again: if the task is "
    "not complete, try a different approach or different arguments instead of "
    "repeating the call."
)


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
        """Record a call. Return reminder text at a threshold, otherwise None."""
        if not tool_name or tool_name.lower() in self.exclude:
            return None
        canonical = canonicalize_arguments(arguments)
        key = (tool_name, canonical)
        if key == self._key:
            self._count += 1
        else:
            self._key = key
            self._count = 1
        if self._count not in self.thresholds:
            return None
        if self._count == self.thresholds[0]:
            return GENTLE_REMINDER
        return detailed_reminder(tool_name, self._count, canonical)

    def reset(self) -> None:
        """Reset the consecutive-call tracking chain (e.g. on new user turns)."""
        self._key = None
        self._count = 0

