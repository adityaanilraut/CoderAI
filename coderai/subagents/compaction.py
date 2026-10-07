"""Tool-free context distillation for an independent child conversation."""

from __future__ import annotations

import copy
import json
from collections.abc import Awaitable, Callable
from typing import Any

from coderai.llm import estimate_openai_request_tokens
from coderai.soul.compaction import COMPACTION_DIRECTIVE_TEMPLATE


async def compact_child_messages(
    messages: list[dict[str, Any]],
    *,
    model: str,
    complete: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    summary_budget: int = 8192,
    input_budget: int = 32768,
) -> dict[str, Any] | None:
    """Commit only a smaller, complete summary across a safe tool boundary.

    The original task, newest steering message, newest assistant/tool group
    and system instructions survive verbatim. Failed summarization never
    replaces the child's existing context.
    """
    assistant_indices = [i for i, msg in enumerate(messages) if msg.get("role") == "assistant"]
    if len(assistant_indices) < 2:
        return None
    cutoff = assistant_indices[-1]
    original = copy.deepcopy(messages)
    history = original[:cutoff]
    pending: set[str] = set()
    for message in history:
        if message.get("role") == "assistant":
            pending.update(str(call.get("id", "")) for call in message.get("tool_calls") or [])
        elif message.get("role") == "tool":
            pending.discard(str(message.get("tool_call_id", "")))
    if pending:
        return None
    systems = [msg for msg in original if msg.get("role") == "system"]
    user_indices = [i for i, msg in enumerate(original) if msg.get("role") == "user"]
    preserved_user_indices = {user_indices[0], user_indices[-1]} if user_indices else set()
    user_messages = [original[i] for i in sorted(preserved_user_indices) if i < cutoff]
    # Keep complete assistant/tool groups while bounding provider input. The
    # original task and newest steering remain pinned outside the summary.
    groups: list[list[dict[str, Any]]] = []
    for entry in history:
        if entry.get("role") != "tool" or not groups:
            groups.append([])
        groups[-1].append(entry)
    selected: list[dict[str, Any]] = []
    for group in reversed(groups):
        candidate_input = group + selected
        if estimate_openai_request_tokens(candidate_input) > input_budget:
            break
        selected = candidate_input
    if not selected:
        return None
    request: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": COMPACTION_DIRECTIVE_TEMPLATE},
            {"role": "user", "content": json.dumps(selected, ensure_ascii=False)},
        ],
        "tool_choice": "none",
        "max_tokens": summary_budget,
    }
    try:
        response = await complete(request)
    except Exception:
        return None
    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    summary = str(message.get("content") or "").strip()
    if (
        not summary
        or message.get("tool_calls")
        or message.get("refusal")
        or choice.get("finish_reason") in ("length", "max_tokens", "content_filter")
    ):
        return None
    candidate = (
        systems
        + user_messages
        + [{"role": "user", "content": f"[Subagent context checkpoint]\n{summary}"}]
        + original[cutoff:]
    )
    if messages != original or estimate_openai_request_tokens(
        candidate
    ) >= estimate_openai_request_tokens(original):
        return None
    messages[:] = candidate
    return response.get("usage") or {}
