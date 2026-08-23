"""Append-only session log helpers: deriveMessages() and pairing-balanced tool-result pruning.

Legacy message-based ``derive_messages()`` is preserved for backward compat.
New code should prefer ``derive_messages_from_events()`` from ``coderai.core.events``.
"""

from __future__ import annotations

from typing import Any

from coderai.core.compaction import ToolResultPruner, DEFAULT_MAX_TOOL_RESULT_CHARS

MAX_TOOL_RESULT_CHARS = DEFAULT_MAX_TOOL_RESULT_CHARS


def _meta(message: Any) -> dict[str, Any]:
    meta = getattr(message, "meta", None)
    return meta if isinstance(meta, dict) else {}


def derive_messages(messages: list[Any]) -> list[Any]:
    """Project the append-only log into model-visible history.

    Compact summary events hide the replaced id range without mutating old rows.
    Legacy logs that flipped `compacted` on old rows still drop those rows.
    """
    replaced: set[str] = set()
    for message in messages:
        meta = _meta(message)
        if meta.get("kind") == "compact/summary" or meta.get("isSummary"):
            for item in meta.get("replacedIds") or []:
                if isinstance(item, str):
                    replaced.add(item)
    visible: list[Any] = []
    for message in messages:
        msg_id = getattr(message, "id", None)
        if isinstance(msg_id, str) and msg_id in replaced:
            continue
        meta = _meta(message)
        if getattr(message, "compacted", False) and not meta.get("isSummary"):
            continue
        visible.append(message)
    return prune_tool_results(visible)


def prune_tool_results(
    messages: list[Any],
    max_chars: int = MAX_TOOL_RESULT_CHARS,
) -> list[Any]:
    """Truncate oversized tool payloads in place on copies; keep pairing intact."""
    return ToolResultPruner(max_chars=max_chars).prune_messages(messages)
