from __future__ import annotations

from typing import Any

from coderai.notifications.models import NotificationView


def to_wire_notification(view: NotificationView) -> Any:
    """Convert to a wire ``Notification`` event."""
    from coderai.wire.types import Notification

    event = view.event
    return Notification(
        id=event.id,
        category=event.category,
        type=event.type,
        source_kind=event.source_kind,
        source_id=event.source_id,
        title=event.title,
        body=event.body,
        severity=event.severity,
        created_at=event.created_at,
        payload=dict(event.payload),
    )
