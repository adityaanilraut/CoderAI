"""Toast dedup — Phase5 port of Kimi ui/shell/prompt.py:1131 toast().

Per left/right deque, topic dedup, immediate prepend.
ponytail: no UI rendering; just queue management for approval/question dedup.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Literal

_IDLE_REFRESH_INTERVAL = 1.0


@dataclass(slots=True)
class _ToastEntry:
    topic: str | None
    message: str
    expires_at: float


_toast_queues: dict[Literal["left", "right"], deque[_ToastEntry]] = {
    "left": deque(),
    "right": deque(),
}


def toast(
    message: str,
    duration: float = 5.0,
    topic: str | None = None,
    immediate: bool = False,
    position: Literal["left", "right"] = "left",
) -> None:
    q = _toast_queues[position]
    duration = max(duration, _IDLE_REFRESH_INTERVAL)
    entry = _ToastEntry(topic=topic, message=message, expires_at=time.monotonic() + duration)
    if topic is not None:
        for existing in list(q):
            if existing.topic == topic:
                q.remove(existing)
    if immediate:
        q.appendleft(entry)
    else:
        q.append(entry)


def _current_toast(position: Literal["left", "right"] = "left") -> _ToastEntry | None:
    q = _toast_queues[position]
    now = time.monotonic()
    while q and q[0].expires_at <= now:
        q.popleft()
    if not q:
        return None
    return q[0]


def clear_toasts(position: Literal["left", "right"] | None = None) -> None:
    if position is None:
        for q in _toast_queues.values():
            q.clear()
    else:
        _toast_queues[position].clear()


# compat aliases
_toast_queues_left = _toast_queues["left"]
_toast_queues_right = _toast_queues["right"]
