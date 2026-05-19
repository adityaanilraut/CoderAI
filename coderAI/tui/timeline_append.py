"""Cap timeline length and insert a trim separator."""

from __future__ import annotations

from typing import Any, Callable, Dict, List

MAX_TIMELINE = 500
KEEP_AFTER_TRIM = 400


def append_capped(
    timeline: List[Dict[str, Any]],
    item: Dict[str, Any],
    next_id: Callable[[], str],
) -> List[Dict[str, Any]]:
    out = timeline + [item]
    if len(out) <= MAX_TIMELINE:
        return out
    trimmed = out[-KEEP_AFTER_TRIM:]
    sep = {
        "kind": "separator",
        "id": next_id(),
        "message": f"History trimmed ({len(out) - KEEP_AFTER_TRIM} older messages hidden)",
    }
    return [sep] + trimmed
