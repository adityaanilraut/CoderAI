"""Context management and token accounting for CoderAI."""

import json
import logging
from typing import Any, Dict, List, Optional

from .events import event_emitter

logger = logging.getLogger(__name__)

# Reserved tokens for response and tool overhead
RESPONSE_TOKEN_RESERVE = 1024
TOOL_OVERHEAD_TOKENS = 512
IMAGE_TOKEN_FLOOR = 1500

class ContextController:
    """Handles token estimation, context window truncation, and summarization."""

    def __init__(self, config, provider, cost_tracker=None):
        self.config = config
        self.provider = provider
        self.cost_tracker = cost_tracker
        # Optional callback(agent, input_delta, output_delta) invoked after
        # LLM summarization so the agent's cumulative token counters stay in
        # sync with the cost tracker.
        self._on_summary_tokens: Optional[callable] = None
        # Cache keyed by content fingerprint. id(msg) was unsafe because
        # CPython recycles freed object ids — Session.get_messages_for_api()
        # rebuilds dicts each turn, so an id can land on a *different* message
        # and serve a stale token count.
        self._token_cache: Dict[str, int] = {}
        self._last_summary_time: float = 0.0
        self._summary_snapshot_input: int = 0
        self._summary_snapshot_output: int = 0

    @staticmethod
    def _msg_fingerprint(msg: Dict[str, Any]) -> str:
        """Stable content-derived key for the token cache."""
        return json.dumps(
            {
                "role": msg.get("role"),
                "content": msg.get("content"),
                "tool_calls": msg.get("tool_calls"),
                "tool_call_id": msg.get("tool_call_id"),
                "name": msg.get("name"),
            },
            default=str,
            sort_keys=True,
        )

    def _estimate_message_tokens(self, msg: Dict[str, Any]) -> int:
        """Estimate tokens for a single message, populating the cache."""
        key = self._msg_fingerprint(msg)
        cached = self._token_cache.get(key)
        if cached is not None:
            return cached
        total = 4  # per-message formatting overhead
        content = msg.get("content") or ""
        if isinstance(content, str) and content:
            total += self.provider.count_tokens(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    block_type = block.get("type", "")
                    if block_type == "image" or block_type == "input_image":
                        total += IMAGE_TOKEN_FLOOR
                    elif block_type == "text" and "text" in block:
                        total += self.provider.count_tokens(block["text"])
                    elif block_type == "image_url":
                        total += IMAGE_TOKEN_FLOOR
                    elif block_type == "tool_result":
                        content_text = block.get("content", "")
                        if isinstance(content_text, str):
                            total += self.provider.count_tokens(content_text)
                        else:
                            total += self.provider.count_tokens(json.dumps(block, default=str))
                    else:
                        total += self.provider.count_tokens(json.dumps(block, default=str))
                else:
                    total += self.provider.count_tokens(str(block))
        if msg.get("tool_calls"):
            total += self.provider.count_tokens(json.dumps(msg["tool_calls"]))
        if msg.get("tool_call_id"):
            total += self.provider.count_tokens(msg["tool_call_id"])
        if msg.get("name"):
            total += self.provider.count_tokens(msg["name"])
        self._token_cache[key] = total
        return total

    def estimate_tokens(self, messages: List[Dict[str, Any]]) -> int:
        """Estimate token count for a list of messages."""
        total = 0
        for msg in messages:
            total += self._estimate_message_tokens(msg)
        total += 3  # 3 tokens reserved for assistant reply priming overhead
        return total

    # Structured marker key used to identify injected pinned-context messages.
    # Using a dict-key marker (instead of a substring of ``content``) means a
    # user message that happens to quote the literal "[Pinned Context]" text
    # is no longer mis-stripped on the next inject pass.
    _CONTEXT_TAG = "[Pinned Context]"
    _CONTEXT_MARKER_KEY = "_pinned_context"

    @classmethod
    def _is_pinned_injection(cls, msg: Dict[str, Any]) -> bool:
        return bool(msg.get(cls._CONTEXT_MARKER_KEY)) and msg.get("role") == "system"

    @classmethod
    def strip_internal_markers(cls, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Return a copy of *messages* with internal-only keys removed.

        Use this just before handing the list to a provider — providers reject
        unknown top-level keys (OpenAI, Anthropic, etc.).
        """
        cleaned: List[Dict[str, Any]] = []
        for m in messages:
            if cls._CONTEXT_MARKER_KEY in m:
                m = {k: v for k, v in m.items() if k != cls._CONTEXT_MARKER_KEY}
            cleaned.append(m)
        return cleaned

    def inject_context(
        self,
        messages: List[Dict[str, Any]],
        context_manager: Any,
        query: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Inject the pinned-context system message after the last system message.

        Returns a *new* list — the caller's list is never mutated. Any
        previously injected context messages (identified by the
        ``_CONTEXT_MARKER_KEY`` flag) are stripped first to prevent
        accumulation across loop iterations.
        """
        context_msg = context_manager.get_system_message(
            query=query,
            messages=messages,
        )
        if not context_msg:
            # Still strip stale context injections even when there's nothing new
            return [m for m in messages if not self._is_pinned_injection(m)]

        # Work on a copy and strip previous injections
        result = [m for m in messages if not self._is_pinned_injection(m)]

        insert_idx = 0
        for i, msg in enumerate(result):
            if msg.get("role") == "system":
                insert_idx = i + 1

        # Keep the human-readable header in ``content`` so logs/transcripts
        # remain readable; deduplication relies on the marker key, not the text.
        tagged_content = f"{self._CONTEXT_TAG}\n{context_msg}"
        result.insert(insert_idx, {
            "role": "system",
            "content": tagged_content,
            self._CONTEXT_MARKER_KEY: True,
        })
        return result

    async def manage_context_window(
        self,
        messages: List[Dict[str, Any]],
        context_limit_override: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Manage context window by summarizing old messages to fit."""
        import time as _time_module

        context_limit = context_limit_override or self.config.context_window
        max_content_tokens = context_limit - RESPONSE_TOKEN_RESERVE - TOOL_OVERHEAD_TOKENS

        if max_content_tokens <= 0:
            max_content_tokens = context_limit // 2

        total_chars = sum(len(str(m.get("content") or "")) for m in messages)
        tool_call_chars = sum(
            len(str(m.get("tool_calls") or "")) + len(str(m.get("tool_call_id") or ""))
            for m in messages
        )
        estimated_tokens_cheap = (total_chars + tool_call_chars) // 4 + len(messages) * 4
        if estimated_tokens_cheap < max_content_tokens * 0.75:
            return messages

        total_tokens = self.estimate_tokens(messages)
        if total_tokens <= max_content_tokens:
            return messages

        self._token_cache.clear()

        logger.info(
            f"Context window management: {total_tokens} tokens exceeds limit of {max_content_tokens}. Truncating old messages."
        )

        system_messages = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]

        # Preserve the very first user message (initial prompt)
        first_task_message = None
        if non_system and non_system[0].get("role") == "user":
            first_task_message = non_system.pop(0)

        system_tokens = sum(self._estimate_message_tokens(m) for m in system_messages)
        if first_task_message:
            system_tokens += self._estimate_message_tokens(first_task_message)
        remaining_budget = max_content_tokens - system_tokens

        # A small slop on the budget so a single estimated token doesn't flip
        # which message groups are kept. Token estimation is approximate, so a
        # razor-sharp boundary is misleading.
        CONTEXT_MARGIN_TOKENS = 200
        effective_budget = max(0, remaining_budget - CONTEXT_MARGIN_TOKENS)

        groups = self._group_messages_for_truncation(non_system)
        kept_groups = []
        running_tokens = 0
        for group in reversed(groups):
            group_tokens = sum(self._estimate_message_tokens(m) for m in group)
            if running_tokens + group_tokens > effective_budget:
                break
            kept_groups.insert(0, group)
            running_tokens += group_tokens

        # Always keep at least the last group (most recent tool interaction)
        if not kept_groups and groups:
            kept_groups = [groups[-1]]

        kept_messages = [msg for group in kept_groups for msg in group]

        if len(kept_messages) < len(non_system):
            removed_messages = non_system[: -len(kept_messages)] if kept_messages else non_system
            text_to_summarize = ""
            for msg in removed_messages:
                role = msg.get("role", "unknown")
                content = msg.get("content")
                if content and isinstance(content, str):
                    text_to_summarize += f"{role.upper()}: {content}\n"

            # Skip LLM summarization when it's unlikely to be worth the cost
            should_summarize = (
                len(removed_messages) > 2
                and len(text_to_summarize) >= 500
                and (_time_module.time() - self._last_summary_time) >= 60
            )

            if should_summarize and text_to_summarize:
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
                    self._last_summary_time = _time_module.time()
                    response = await self.provider.chat([{"role": "user", "content": prompt}], tools=None)
                    summary_content = ""
                    if "choices" in response and response["choices"]:
                        summary_content = response["choices"][0].get("message", {}).get("content", "")

                    if summary_content and self.cost_tracker is not None:
                        mi = self.provider.get_model_info()
                        in_tok = mi.get("total_input_tokens", 0)
                        out_tok = mi.get("total_output_tokens", 0)
                        in_tok_delta = max(0, in_tok - self._summary_snapshot_input)
                        out_tok_delta = max(0, out_tok - self._summary_snapshot_output)
                        self._summary_snapshot_input = in_tok
                        self._summary_snapshot_output = out_tok
                        if in_tok_delta or out_tok_delta:
                            model_for_cost = getattr(self.provider, "actual_model", self.config.default_model)
                            self.cost_tracker.add_cost(model_for_cost, in_tok_delta, out_tok_delta)
                            if self._on_summary_tokens is not None:
                                self._on_summary_tokens(in_tok_delta, out_tok_delta)

                    if summary_content:
                        summary_notice = {
                            "role": "system",
                            "content": f"[Prior Conversation Summary]: {summary_content}",
                        }
                        summary_tokens = self._estimate_message_tokens(summary_notice)
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

        import copy

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
        summarized = truncate_recursive(copy.deepcopy(result), per_string_max)

        aggregate_cap = self.config.max_tool_output * 2
        final_str = json.dumps(summarized)
        if len(final_str) > aggregate_cap:
            safe_output = final_str[:aggregate_cap]
            last_comma = safe_output.rfind('",')
            last_brace = safe_output.rfind('}')
            cut = max(last_comma, last_brace)
            if cut > aggregate_cap // 2:
                safe_output = safe_output[: cut + 1]
            safe_output += "\n... [HARD TRUNCATED]"

            return {
                "success": False,
                "error": "TOOL OUTPUT TOO LARGE",
                "warning": (
                    "Tool output was extremely large and was forcefully truncated. "
                    "DO NOT repeat the exact same tool call. Use a more specific "
                    "method to extract the data (like grep or specific search keywords)."
                ),
                "output": safe_output,
            }

        return summarized
