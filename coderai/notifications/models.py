"""Task/agent/system notifications.

Events persist under ``<project>/.coderai/notifications/<id>/{event,delivery}.json``
with per-sink ``pending|claimed|acked`` delivery states. Sinks:

- ``llm`` — delivered into the next turn as an advisory user message
  (``SessionManager._activate`` claims up to 4)
- ``wire`` — forwarded as wire ``Notification`` events for wire clients
- ``shell`` — claimable by interactive UIs (toasts)

Delivery is dedup-safe: notification ids already present in history
(``<notification id="...">``) are acked without re-appending.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from coderai.utils.io import atomic_json_write

NotificationCategory = str  # "task" | "agent" | "system"
NotificationSeverity = str  # "info" | "success" | "warning" | "error"
NotificationSink = str  # "llm" | "wire" | "shell"
NotificationDeliveryStatus = str  # "pending" | "claimed" | "acked"

DEFAULT_SINKS: tuple[str, ...] = ("llm", "wire", "shell")
CLAIM_STALE_AFTER_S = 15.0  # Claim timeout in seconds
DELIVER_LIMIT = 8  # per-sink claim cap; turns consume at most 4
TURN_DELIVER_LIMIT = 4

_VALID_ID = re.compile(r"^[a-z0-9]{2,20}$")
_NOTIFICATION_ID_RE = re.compile(r'<notification id="([^"]+)"')


@dataclass
class NotificationEvent:
    version: int = 1
    id: str = ""
    category: NotificationCategory = "system"
    type: str = ""
    source_kind: str = ""
    source_id: str = ""
    title: str = ""
    body: str = ""
    severity: NotificationSeverity = "info"
    created_at: float = field(default_factory=time.time)
    payload: dict[str, Any] = field(default_factory=dict)
    targets: list[NotificationSink] = field(default_factory=lambda: list(DEFAULT_SINKS))
    dedupe_key: str | None = None


@dataclass
class NotificationSinkState:
    status: NotificationDeliveryStatus = "pending"
    claimed_at: float | None = None
    acked_at: float | None = None


@dataclass
class NotificationDelivery:
    sinks: dict[str, NotificationSinkState] = field(default_factory=dict)


@dataclass
class NotificationView:
    event: NotificationEvent
    delivery: NotificationDelivery


def _validate_notification_id(notification_id: str) -> None:
    if not _VALID_ID.match(notification_id):
        raise ValueError(f"Invalid notification_id: {notification_id!r}")


def _event_to_dict(event: NotificationEvent) -> dict[str, Any]:
    return asdict(event)


def _event_from_dict(data: dict[str, Any]) -> NotificationEvent:
    event = NotificationEvent()
    for key in (
        "version",
        "id",
        "category",
        "type",
        "source_kind",
        "source_id",
        "title",
        "body",
        "severity",
        "created_at",
        "payload",
        "targets",
        "dedupe_key",
    ):
        if key in data:
            setattr(event, key, data[key])
    return event


def _delivery_to_dict(delivery: NotificationDelivery) -> dict[str, Any]:
    return {"sinks": {k: asdict(v) for k, v in delivery.sinks.items()}}


def _delivery_from_dict(data: dict[str, Any]) -> NotificationDelivery:
    sinks: dict[str, NotificationSinkState] = {}
    raw = data.get("sinks") if isinstance(data, dict) else None
    if isinstance(raw, dict):
        for sink, state in raw.items():
            st = NotificationSinkState()
            if isinstance(state, dict):
                for key in ("status", "claimed_at", "acked_at"):
                    if key in state:
                        setattr(st, key, state[key])
            sinks[str(sink)] = st
    return NotificationDelivery(sinks=sinks)
