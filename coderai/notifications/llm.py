# Ported from coderai/core/notifications.py - kimi structure (notifications/llm.py).
from __future__ import annotations

from coderai.notifications.models import NotificationView, _NOTIFICATION_ID_RE
def build_notification_message(view: NotificationView) -> str:
    """Render the advisory user-message text for the ``llm`` sink (Kimi parity)."""
    event = view.event
    lines = [
        f'<notification id="{event.id}" category="{event.category}" '
        f'type="{event.type}" source_kind="{event.source_kind}" source_id="{event.source_id}">',
        f"Title: {event.title}",
        f"Severity: {event.severity}",
        event.body,
        "</notification>",
    ]
    return "\n".join(lines)


def extract_notification_ids(contents: list[str]) -> set[str]:
    """Ids already present in history (skip re-delivery, Kimi parity)."""
    ids: set[str] = set()
    for content in contents:
        for match in _NOTIFICATION_ID_RE.finditer(content or ""):
            ids.add(match.group(1))
    return ids
