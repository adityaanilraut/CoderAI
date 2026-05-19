"""Which timeline items are safe to treat as immutable after completion."""

from __future__ import annotations

from typing import Any, Dict

TIMELINE_ITEM_KINDS = frozenset(
    {
        "user",
        "assistant",
        "tool",
        "diff",
        "error",
        "toast",
        "separator",
        "approval",
    }
)


def is_timeline_item_frozen(item: Dict[str, Any]) -> bool:
    """Return True when a timeline row should not be redrawn on every tick."""
    kind = item.get("kind")
    if kind == "assistant":
        return not item.get("streaming", False)
    if kind == "tool":
        return item.get("ok") is not None
    if kind == "approval":
        return item.get("decided") != "pending"
    if kind in ("user", "diff", "error", "toast", "separator"):
        return True
    return False
