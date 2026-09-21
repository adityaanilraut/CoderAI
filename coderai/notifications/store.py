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

from pathlib import Path

from coderai.utils.io import atomic_json_write
from coderai.notifications.models import (
    NotificationDelivery,
    NotificationEvent,
    NotificationView,
    _delivery_from_dict,
    _delivery_to_dict,
    _event_from_dict,
    _event_to_dict,
    _validate_notification_id,
)


class NotificationStore:
    """Per-notification ``event.json`` + ``delivery.json`` persistence."""

    EVENT_FILE = "event.json"
    DELIVERY_FILE = "delivery.json"

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    def _notification_dir(self, notification_id: str) -> Path:
        _validate_notification_id(notification_id)
        path = self._root / notification_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def create_notification(self, event: NotificationEvent, delivery: NotificationDelivery) -> None:
        directory = self._notification_dir(event.id)
        atomic_json_write(_event_to_dict(event), directory / self.EVENT_FILE)
        atomic_json_write(_delivery_to_dict(delivery), directory / self.DELIVERY_FILE)

    def list_notification_ids(self) -> list[str]:
        if not self._root.exists():
            return []
        ids: list[str] = []
        try:
            entries = sorted(self._root.iterdir())
        except OSError:
            return []
        for path in entries:
            if path.is_dir() and (path / self.EVENT_FILE).exists():
                ids.append(path.name)
        return ids

    def read_event(self, notification_id: str) -> NotificationEvent:
        import json

        _validate_notification_id(notification_id)
        raw = (self._root / notification_id / self.EVENT_FILE).read_text(encoding="utf-8")
        return _event_from_dict(json.loads(raw))

    def read_delivery(self, notification_id: str) -> NotificationDelivery:
        import json

        path = self._root / notification_id / self.DELIVERY_FILE
        if not path.exists():
            return NotificationDelivery()
        try:
            return _delivery_from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, UnicodeDecodeError):
            return NotificationDelivery()

    def write_delivery(self, notification_id: str, delivery: NotificationDelivery) -> None:
        _validate_notification_id(notification_id)
        atomic_json_write(
            _delivery_to_dict(delivery),
            self._root / notification_id / self.DELIVERY_FILE,
        )

    def merged_view(self, notification_id: str) -> NotificationView:
        return NotificationView(
            event=self.read_event(notification_id),
            delivery=self.read_delivery(notification_id),
        )

    def list_views(self) -> list[NotificationView]:
        views: list[NotificationView] = []
        for notification_id in self.list_notification_ids():
            try:
                views.append(self.merged_view(notification_id))
            except (OSError, ValueError, UnicodeDecodeError):
                continue
        views.sort(key=lambda view: view.event.created_at, reverse=True)
        return views
