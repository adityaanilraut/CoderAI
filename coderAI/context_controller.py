"""Context management and token accounting for CoderAI."""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from .events import event_emitter

logger = logging.getLogger(__name__)

# #region agent log
_CTX_DEBUG_LOG_PATHS = (
    Path(__file__).resolve().parent.parent / ".cursor" / "debug-d1bd12.log",
    Path.home() / ".coderAI" / "debug-d1bd12.log",
)


def _ctx_debug_log(hypothesis_id: str, location: str, message: str, data: dict) -> None:
    import time as _t

    line = (
        json.dumps(
            {
                "sessionId": "d1bd12",
                "runId": "pre-fix",
                "hypothesisId": hypothesis_id,
                "location": location,
                "message": message,
                "data": data,
                "timestamp": int(_t.time() * 1000),
            },
            default=str,
        )
        + "\n"
    )
    for _p in _CTX_DEBUG_LOG_PATHS:
        try:
            _p.parent.mkdir(parents=True, exist_ok=True)
            with open(_p, "a", encoding="utf-8") as _f:
                _f.write(line)
        except Exception:
            pass


# #endregion

# Reserved tokens for response and tool overhead
RESPONSE_TOKEN_RESERVE = 1024
TOOL_OVERHEAD_TOKENS = 512
IMAGE_TOKEN_FLOOR = 1500

class ContextController:
    """Handles token estimation, context window truncation, and summarization."""

    def __init__(self, config, provider):
        self.config = config
        self.provider = provider

    def estimate_tokens(self, messages: List[Dict[str, Any]]) -> int:
        """Estimate token count for a list of messages."""
        total = 0
        for msg in messages:
            # ~4 tokens per message for formatting overhead (role, separators, etc.)
            total += 4
            content = msg.get("content") or ""
            if isinstance(content, str) and content:
                total += self.provider.count_tokens(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") in {"image", "input_image"}:
                        total += IMAGE_TOKEN_FLOOR
                    else:
                        total += self.provider.count_tokens(json.dumps(block, default=str))
            # Tool calls add tokens too
            if msg.get("tool_calls"):
                total += self.provider.count_tokens(json.dumps(msg["tool_calls"]))
            # Tool result metadata
            if msg.get("tool_call_id"):
                total += self.provider.count_tokens(msg["tool_call_id"])
            if msg.get("name"):
                total += self.provider.count_tokens(msg["name"])
        total += 3  # reply priming
        return total

    # Tag used to identify injected context messages for deduplication
    _CONTEXT_TAG = "[Pinned Context]"

    def inject_context(
        self, 
        messages: List[Dict[str, Any]], 
        context_manager: Any,
        query: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Inject the pinned-context system message after the last system message.

        Returns a *new* list — the caller's list is never mutated. Any
        previously injected context messages (identified by ``_CONTEXT_TAG``)
        are stripped first to prevent accumulation across loop iterations.
        """
        context_msg = context_manager.get_system_message(
            query=query,
            messages=messages,
        )
        if not context_msg:
            # Still strip stale context injections even when there's nothing new
            return [
                m for m in messages
                if not (m.get("role") == "system" and isinstance(m.get("content"), str)
                        and self._CONTEXT_TAG in m["content"])
            ]

        # Work on a copy and strip previous injections
        result = [
            m for m in messages
            if not (m.get("role") == "system" and isinstance(m.get("content"), str)
                    and self._CONTEXT_TAG in m["content"])
        ]

        insert_idx = 0
        for i, msg in enumerate(result):
            if msg.get("role") == "system":
                insert_idx = i + 1

        tagged_content = f"{self._CONTEXT_TAG}\n{context_msg}"
        result.insert(insert_idx, {"role": "system", "content": tagged_content})
        return result

    async def manage_context_window(
        self,
        messages: List[Dict[str, Any]],
        context_limit_override: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Manage context window by summarizing old messages to fit."""
        context_limit = context_limit_override or self.config.context_window
        max_content_tokens = context_limit - RESPONSE_TOKEN_RESERVE - TOOL_OVERHEAD_TOKENS

        if max_content_tokens <= 0:
            max_content_tokens = context_limit // 2

        total_tokens = self.estimate_tokens(messages)
        if total_tokens <= max_content_tokens:
            return messages

        # #region agent log
        _ctx_debug_log(
            "A",
            "context_controller.py:manage_context_window",
            "truncation_triggered",
            {
                "total_tokens": total_tokens,
                "max_content_tokens": max_content_tokens,
                "context_limit": context_limit,
            },
        )
        # #endregion

        logger.info(
            f"Context window management: {total_tokens} tokens exceeds limit of {max_content_tokens}. Truncating old messages."
        )

        system_messages = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]

        # Preserve the very first user message (initial prompt)
        first_task_message = None
        if non_system and non_system[0].get("role") == "user":
            first_task_message = non_system.pop(0)

        system_tokens = self.estimate_tokens(system_messages + ([first_task_message] if first_task_message else []))
        remaining_budget = max_content_tokens - system_tokens

        groups = self._group_messages_for_truncation(non_system)
        kept_groups = []
        running_tokens = 0
        for group in reversed(groups):
            group_tokens = self.estimate_tokens(group)
            if running_tokens + group_tokens > remaining_budget:
                break
            kept_groups.insert(0, group)
            running_tokens += group_tokens

        # Always keep at least the last group (most recent tool interaction)
        # so the model retains context about what just happened, even if it
        # exceeds the budget.  Without this, tool-heavy sessions can end up
        # with zero kept messages and a confused model.
        if not kept_groups and groups:
            kept_groups = [groups[-1]]

        kept_messages = [msg for group in kept_groups for msg in group]

        if len(kept_messages) < len(non_system):
            removed_messages = non_system[: -len(kept_messages)] if kept_messages else non_system
            # #region agent log
            _ctx_debug_log(
                "A",
                "context_controller.py:truncation_detail",
                "messages_removed_for_window",
                {
                    "removed_count": len(removed_messages),
                    "kept_non_system_count": len(kept_messages),
                    "groups_total": len(groups),
                    "kept_groups_count": len(kept_groups),
                },
            )
            # #endregion
            text_to_summarize = ""
            for msg in removed_messages:
                role = msg.get("role", "unknown")
                content = msg.get("content")
                if content and isinstance(content, str):
                    text_to_summarize += f"{role.upper()}: {content}\n"

            if text_to_summarize:
                event_emitter.emit(
                    "agent_status",
                    message="\n[dim]Context window filling up. Summarizing older conversations...[/dim]",
                )
                prompt = (
                    "Summarize the following conversation history. This summary will replace "
                    "these messages in our memory, so preserve ALL of the following:\n"
                    "- Tool calls made and their outcomes (file paths, commands run, results)\n"
                    "- Files read, created, or modified (with paths)\n"
                    "- Key decisions made and their rationale\n"
                    "- Errors encountered and how they were resolved\n"
                    "- User preferences or constraints stated\n"
                    "- Current task status and next steps\n"
                    "Be concise but factually complete.\n\n"
                    f"{text_to_summarize}"
                )
                try:
                    response = await self.provider.chat([{"role": "user", "content": prompt}], tools=None)
                    summary_content = ""
                    if "choices" in response and response["choices"]:
                        summary_content = response["choices"][0].get("message", {}).get("content", "")

                    if summary_content:
                        summary_notice = {
                            "role": "system",
                            "content": f"[Prior Conversation Summary]: {summary_content}",
                        }
                        summary_tokens = self.estimate_tokens([summary_notice])
                        if running_tokens + summary_tokens <= remaining_budget:
                            result = system_messages + ([first_task_message] if first_task_message else []) + [summary_notice] + kept_messages
                            return result
                        logger.info("Skipping generated summary because it would overflow the remaining context budget.")
                except Exception as e:
                    logger.warning(f"Failed to summarize context: {e}")

            truncation_notice = {
                "role": "system",
                "content": f"[Note: {len(removed_messages)} earlier messages were removed to fit the context window. The conversation continues from here.]",
            }
            return system_messages + ([first_task_message] if first_task_message else []) + [truncation_notice] + kept_messages

        return system_messages + ([first_task_message] if first_task_message else []) + kept_messages

    def _group_messages_for_truncation(self, messages: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        """Group messages into atomic units for safe truncation."""
        groups = []
        i = 0
        while i < len(messages):
            msg = messages[i]
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                group = [msg]
                i += 1
                while i < len(messages) and messages[i].get("role") == "tool":
                    group.append(messages[i])
                    i += 1
                groups.append(group)
            else:
                groups.append([msg])
                i += 1
        return groups

    def summarize_tool_result(self, result: Any) -> Dict[str, Any]:
        """Summarize large tool results to prevent context overflow."""
        if not isinstance(result, dict):
            return {
                "success": False,
                "error": str(result) if result is not None else "No result returned",
            }

        def truncate_recursive(val, max_len):
            if isinstance(val, str):
                if len(val) > max_len:
                    half = max_len // 2
                    return val[:half] + f"\n... [{len(val) - 2 * half} chars truncated] ...\n" + val[-half:]
                return val
            elif isinstance(val, list):
                if len(val) > 50:
                    return [truncate_recursive(v, max_len) for v in val[:50]] + [{"_note": f"Showing 50 of {len(val)} items"}]
                return [truncate_recursive(v, max_len) for v in val]
            elif isinstance(val, dict):
                return {k: truncate_recursive(v, max_len) for k, v in val.items()}
            return val

        per_string_max = self.config.max_tool_output // 2
        summarized = truncate_recursive(result, per_string_max)

        aggregate_cap = self.config.max_tool_output * 2
        final_str = json.dumps(summarized)
        if len(final_str) > aggregate_cap:
            return {
                "error": "TOOL OUTPUT TOO LARGE",
                "warning": (
                    "Tool output was extremely large and was forcefully truncated. "
                    "DO NOT repeat the exact same tool call. Use a more specific "
                    "method to extract the data (like grep or specific search keywords)."
                ),
                "output": final_str[:aggregate_cap] + "\n... [HARD TRUNCATED]",
            }

        return summarized
