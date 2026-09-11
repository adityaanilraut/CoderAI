# Ported from coderai/core/notifications.py - kimi structure (notifications/manager.py).
"""Task/agent/system notifications (Kimi ``notifications/`` parity, slim).

Events persist under ``<project>/.coderai/notifications/<id>/{event,delivery}.json``
with per-sink ``pending|claimed|acked`` delivery states. Sinks:

- ``llm`` — delivered into the next turn as an advisory user message
  (``SessionManager._activate`` claims up to 4, Kimi ``deliver_pending`` parity)
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
from coderai.notifications.models import (
    CLAIM_STALE_AFTER_S,
    DELIVER_LIMIT,
    NotificationDelivery,
    NotificationEvent,
    NotificationSinkState,
    NotificationView,
)
from coderai.notifications.store import NotificationStore
class NotificationManager:
    """Claim/ack delivery over a :class:`NotificationStore` (Kimi parity)."""

    def __init__(self, root: Path, *, claim_stale_after_s: float = CLAIM_STALE_AFTER_S) -> None:
        self._store = NotificationStore(root)
        self._claim_stale_after_s = claim_stale_after_s

    @property
    def store(self) -> NotificationStore:
        return self._store

    def new_id(self) -> str:
        return f"n{uuid.uuid4().hex[:8]}"

    def publish(self, event: NotificationEvent) -> NotificationView:
        if event.dedupe_key:
            for view in self._store.list_views():
                if view.event.dedupe_key == event.dedupe_key:
                    return view
        delivery = NotificationDelivery(
            sinks={sink: NotificationSinkState() for sink in event.targets}
        )
        self._store.create_notification(event, delivery)
        return NotificationView(event=event, delivery=delivery)

    def recover(self) -> None:
        now = time.time()
        for view in self._store.list_views():
            updated = False
            for sink_state in view.delivery.sinks.values():
                if sink_state.status != "claimed" or sink_state.claimed_at is None:
                    continue
                if now - sink_state.claimed_at <= self._claim_stale_after_s:
                    continue
                sink_state.status = "pending"
                sink_state.claimed_at = None
                updated = True
            if updated:
                self._store.write_delivery(view.event.id, view.delivery)

    def has_pending_for_sink(self, sink: str) -> bool:
        for view in self._store.list_views():
            sink_state = view.delivery.sinks.get(sink)
            if sink_state is not None and sink_state.status == "pending":
                return True
        return False

    def claim_for_sink(self, sink: str, *, limit: int = DELIVER_LIMIT) -> list[NotificationView]:
        self.recover()
        claimed: list[NotificationView] = []
        now = time.time()
        for view in reversed(self._store.list_views()):
            sink_state = view.delivery.sinks.get(sink)
            if sink_state is None or sink_state.status != "pending":
                continue
            sink_state.status = "claimed"
            sink_state.claimed_at = now
            self._store.write_delivery(view.event.id, view.delivery)
            claimed.append(view)
            if len(claimed) >= limit:
                break
        return claimed

    async def deliver_pending(
        self,
        sink: str,
        *,
        on_notification: Callable[[NotificationView], Awaitable[None] | None],
        limit: int = DELIVER_LIMIT,
    ) -> list[NotificationView]:
        """Claim + handle + ack pending notifications for one sink.

        A failing handler leaves the notification ``claimed`` for later
        recovery; delivery continues with the rest (Kimi parity).
        """
        delivered: list[NotificationView] = []
        for view in self.claim_for_sink(sink, limit=limit):
            try:
                result = on_notification(view)
                if result is not None:
                    await result
            except Exception:
                continue
            delivered.append(self.ack(sink, view.event.id))
        return delivered

    def ack(self, sink: str, notification_id: str) -> NotificationView:
        view = self._store.merged_view(notification_id)
        sink_state = view.delivery.sinks.get(sink)
        if sink_state is None:
            return view
        sink_state.status = "acked"
        sink_state.acked_at = time.time()
        sink_state.claimed_at = None
        self._store.write_delivery(notification_id, view.delivery)
        return NotificationView(event=view.event, delivery=view.delivery)

    def ack_ids(self, sink: str, notification_ids: set[str]) -> None:
        for notification_id in notification_ids:
            try:
                self.ack(sink, notification_id)
            except (FileNotFoundError, ValueError, OSError):
                continue
