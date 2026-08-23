"""Repeat Tool Call Detector and Advisory Reminder Guard.

Port of DeepSeek Harness dsh-repeat-tool-reminder. Tracks consecutive identical or near-identical
tool invocations per session and surfaces non-blocking advisory warnings to break execution loops.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_REPEAT_THRESHOLDS = [3, 5, 8]


@dataclass
class ToolCallRecord:
    tool_name: str
    args_hash: str
    count: int = 1


class RepeatToolReminderGuard:
    """Detects consecutive repeat tool calls and generates advisory reminders."""

    def __init__(self, thresholds: list[int] | None = None) -> None:
        self.thresholds = sorted(thresholds or DEFAULT_REPEAT_THRESHOLDS)
        self._history: dict[str, ToolCallRecord] = {}
        self._lock = threading.RLock()

    def _hash_args(self, args: dict[str, Any]) -> str:
        try:
            return json.dumps(args, sort_keys=True)
        except Exception:
            return str(args)

    def record_call(self, session_id: str, tool_name: str, args: dict[str, Any]) -> str | None:
        """Record a tool invocation and return an advisory reminder string if a repeat threshold is crossed."""
        args_hash = self._hash_args(args)
        with self._lock:
            last = self._history.get(session_id)
            if last and last.tool_name == tool_name and last.args_hash == args_hash:
                last.count += 1
                if last.count in self.thresholds:
                    return (
                        f"⚠️ Advisory Notice: You have invoked tool '{tool_name}' with the same arguments "
                        f"{last.count} consecutive times. If the tool is not producing new information or progress, "
                        "please alter your approach, inspect existing files, or re-evaluate the task."
                    )
            else:
                self._history[session_id] = ToolCallRecord(
                    tool_name=tool_name,
                    args_hash=args_hash,
                    count=1,
                )
            return None

    def reset_session(self, session_id: str) -> None:
        with self._lock:
            self._history.pop(session_id, None)


_global_repeat_guard: RepeatToolReminderGuard | None = None


def get_repeat_reminder_guard() -> RepeatToolReminderGuard:
    global _global_repeat_guard
    if _global_repeat_guard is None:
        _global_repeat_guard = RepeatToolReminderGuard()
    return _global_repeat_guard
