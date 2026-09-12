from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from coderai.notifications.models import NotificationView, _NOTIFICATION_ID_RE
def build_notification_message(view: NotificationView) -> str:
    """Render the advisory user-message text for the ``llm`` sink."""
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


def extract_notification_ids(contents: list[str] | Sequence[Any]) -> set[str]:
    """Ids already present in history (skip re-delivery)."""
    ids: set[str] = set()
    for item in contents:
        text = item if isinstance(item, str) else getattr(item, "extract_text", lambda: str(item))()
        for match in _NOTIFICATION_ID_RE.finditer(text or ""):
            ids.add(match.group(1))
    return ids


def is_notification_message(message: Any) -> bool:
    """Return True if message represents a background notification injection."""
    role = getattr(message, "role", None)
    if role != "user":
        return False
    content = getattr(message, "content", None)
    if not content:
        return False
    if isinstance(content, list) and len(content) == 1:
        part = content[0]
        text = getattr(part, "text", str(part))
        return text.lstrip().startswith("<notification ")
    if isinstance(content, str):
        return content.lstrip().startswith("<notification ")
    return False
