"""Append-only session log helpers: deriveMessages() and pairing-balanced tool-result pruning.

Legacy message-based ``derive_messages()`` is preserved for backward compat.
New code should prefer ``derive_messages_from_events()`` from ``coderai.events``.
"""

from __future__ import annotations

from typing import Any

from coderai.soul.compaction import ToolResultPruner, DEFAULT_MAX_TOOL_RESULT_CHARS

MAX_TOOL_RESULT_CHARS = DEFAULT_MAX_TOOL_RESULT_CHARS


def _meta(message: Any) -> dict[str, Any]:
    meta = getattr(message, "meta", None)
    return meta if isinstance(meta, dict) else {}


def derive_messages(messages: list[Any]) -> list[Any]:
    """Project the append-only log into model-visible history.

    Compact summary events hide the replaced id range without mutating old rows.
    Legacy logs that flipped `compacted` on old rows still drop those rows.
    AL-A11: places the summary where the first hidden message was, as a marked
    user message (not mid-conversation system).
    """
    replaced: set[str] = set()
    summary_by_first_id: dict[str, Any] = {}
    summary_messages: list[Any] = []

    for message in messages:
        meta = _meta(message)
        if meta.get("kind") == "compact/summary" or meta.get("isSummary"):
            summary_messages.append(message)
            first_replaced: str | None = None
            for item in meta.get("replacedIds") or []:
                if isinstance(item, str):
                    replaced.add(item)
                    if first_replaced is None:
                        first_replaced = item
            if first_replaced:
                summary_by_first_id[first_replaced] = message

    visible: list[Any] = []
    placed_summaries: set[str] = set()

    for message in messages:
        msg_id = getattr(message, "id", None)
        meta = _meta(message)
        is_summary = meta.get("kind") == "compact/summary" or meta.get("isSummary")

        if isinstance(msg_id, str) and msg_id in summary_by_first_id:
            s_msg = summary_by_first_id[msg_id]
            s_id = getattr(s_msg, "id", None)
            while isinstance(s_id, str) and s_id in summary_by_first_id:
                s_msg = summary_by_first_id[s_id]
                s_id = getattr(s_msg, "id", None)
            if isinstance(s_id, str) and s_id not in placed_summaries:
                placed_summaries.add(s_id)
                visible.append(s_msg)

        if isinstance(msg_id, str) and msg_id in replaced:
            continue
        if is_summary:
            if isinstance(msg_id, str) and msg_id in placed_summaries:
                continue
            if isinstance(msg_id, str):
                placed_summaries.add(msg_id)

        if getattr(message, "compacted", False) and not is_summary:
            continue
        visible.append(message)
    return prune_tool_results(visible)


def prune_tool_results(
    messages: list[Any],
    max_chars: int = MAX_TOOL_RESULT_CHARS,
) -> list[Any]:
    """Truncate oversized tool payloads in place on copies; keep pairing intact."""
    return ToolResultPruner(max_chars=max_chars).prune_messages(messages)
