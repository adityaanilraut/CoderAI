"""Compaction Engine

Provides structured compaction with:
1. Dual triggers: 'pressure' (active token threshold exceeded) and 'overflow' (context window overflow).
2. Range selection that respects tool call/result pairing boundaries.
3. Shadow events (compaction/start, compaction/summary, compaction/end) rather than mutating history.
4. ToolResultPruner for deterministic head/middle/tail pruning of oversized tool output.
5. Abstract CompactionEngine protocol + BasicCompaction implementation.
"""

from __future__ import annotations

import abc
import asyncio
import copy
import re
import uuid
from dataclasses import dataclass, field, is_dataclass, replace
from typing import Any, TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from coderai.soul.session.manager import SessionManager, SessionMessage

DEFAULT_MAX_TOOL_RESULT_CHARS = 32_000


class CompactionMessageConverter(Protocol):
    def convert_session_messages(
        self,
        messages: list[Any],
        *,
        model: str,
        thinking_enabled: bool,
    ) -> list[dict[str, Any]]: ...


COMPACTION_DIRECTIVE_TEMPLATE = (
    "You are now acting as a compaction engine for this AI coding assistant. "
    "Condense the conversation ABOVE into a structured checkpoint that lets another model resume the work with no loss of essential context.\n\n"
    'Output EXACTLY the Markdown structure below: keep every section, in order. Use terse bullets, not prose paragraphs. Write "(none)" for an empty section — never drop a section.\n\n'
    "## Primary Request and Intent\n"
    "- [the user's original and evolving goals; quote verbatim where the exact wording matters; include all explicit user messages]\n\n"
    "## Key Technical Concepts\n"
    "- [technologies, frameworks, patterns, runtime versions, and conventions in play]\n\n"
    "## Files and Code Sections\n"
    "- [exact file paths, functions, and line numbers examined, modified, or created]\n\n"
    "## Errors and Fixes\n"
    "- [all encountered error messages, stack traces, root causes, and verified fixes]\n\n"
    "## Critical Decisions & Constraints\n"
    "- [architectural, design, and implementation decisions made, plan-mode state, and plan file path if active]\n\n"
    "## State of Progress & Completed Tasks\n"
    "- [completed tasks, modified files, verified behaviors, loaded skills, background job IDs, subagent IDs, and todo state]\n\n"
    "## Pending Work & Next Steps\n"
    "- [immediate next actions and known open questions]\n\n"
    "Do not include conversational filler before or after the summary."
)


@dataclass
class CompactionResult:
    """Result of a compaction operation."""

    compaction_id: str
    summary: str
    shadowed_range: dict[str, int]  # {"start": seq_or_idx, "end": seq_or_idx}
    shadowed_ids: list[str] = field(default_factory=list)
    shadowed_seqs: list[int] = field(default_factory=list)
    shadowed_token_count: int = 0
    tokens_after: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "compactionId": self.compaction_id,
            "summary": self.summary,
            "shadowedRange": self.shadowed_range,
            "shadowedIds": self.shadowed_ids,
            "shadowedSeqs": self.shadowed_seqs,
            "shadowedTokenCount": self.shadowed_token_count,
            "tokensAfter": self.tokens_after,
        }


class ToolResultPruner:
    """Deterministic head/middle/tail pruner for tool results."""

    def __init__(self, max_chars: int = DEFAULT_MAX_TOOL_RESULT_CHARS) -> None:
        self.max_chars = max_chars

    def prune_content(self, content: str) -> str:
        """Truncate content exceeding max_chars symmetrically."""
        if not content or len(content) <= self.max_chars:
            return content
        from coderai.utils.common.tool_payload import prune_tool_payload

        return prune_tool_payload(content, self.max_chars)

    def prune_messages(self, messages: list[Any]) -> list[Any]:
        """Prune tool result messages in place or on copies while preserving list structure."""
        out: list[Any] = []
        for msg in messages:
            role = getattr(msg, "role", "") if hasattr(msg, "role") else msg.get("role", "")
            if role != "tool":
                out.append(msg)
                continue
            content = (
                getattr(msg, "content", "") if hasattr(msg, "content") else msg.get("content", "")
            )
            if not isinstance(content, str) or len(content) <= self.max_chars:
                out.append(msg)
                continue
            pruned_text = self.prune_content(content)
            if isinstance(msg, dict):
                out.append({**msg, "content": pruned_text})
            elif is_dataclass(msg) and not isinstance(msg, type):
                out.append(replace(msg, content=pruned_text))
            else:
                clone = copy.copy(msg)
                setattr(clone, "content", pruned_text)
                out.append(clone)
        return out


def prune_tool_results_for_compaction(messages: list[Any], max_chars: int = 1500) -> list[Any]:
    """Helper to prune bulky tool results from a slice of messages before summarization."""
    pruner = ToolResultPruner(max_chars=max_chars)
    return pruner.prune_messages(messages)


def evaluate_compaction_trigger(
    active_tokens: int,
    context_limit: int,
    pressure_ratio: float = 0.75,
    overflow_ratio: float = 0.95,
    reserved_context_size: int | None = None,
) -> str | None:
    """Evaluate whether active tokens meet dual-trigger thresholds ('overflow' vs 'pressure').

    Triggers auto compaction when either
    active_tokens >= context_limit * ratio OR active_tokens + reserved >= context_limit.
    """
    if context_limit <= 0 or active_tokens <= 0:
        return None
    # Reserved budget guard
    if reserved_context_size and 0 < reserved_context_size < context_limit:
        if active_tokens + reserved_context_size >= context_limit:
            return (
                "overflow" if active_tokens >= int(context_limit * overflow_ratio) else "pressure"
            )
    if active_tokens >= int(context_limit * overflow_ratio):
        return "overflow"
    if active_tokens >= int(context_limit * pressure_ratio):
        return "pressure"
    return None


def estimate_text_tokens(messages: Any) -> int:
    """Estimate tokens from message text content and tool calls using a character-based heuristic."""
    total_chars = 0
    non_ascii_count = 0
    media_tokens = 0

    def _add_text(t: str) -> None:
        nonlocal total_chars, non_ascii_count
        if not t:
            return
        ascii_chars = sum(c.isascii() for c in t)
        total_chars += ascii_chars
        non_ascii_count += len(t) - ascii_chars

    for msg in messages:
        content = getattr(msg, "content", None)
        if content is None and isinstance(msg, dict):
            content = msg.get("content")
        if isinstance(content, str):
            _add_text(content)
        elif isinstance(content, (list, tuple)):
            for part in content:
                if hasattr(part, "text"):
                    _add_text(getattr(part, "text", "") or "")
                elif isinstance(part, dict) and "text" in part:
                    _add_text(part.get("text") or "")
                elif isinstance(part, str):
                    _add_text(part)
                if (
                    part.get("type") if isinstance(part, dict) else getattr(part, "type", None)
                ) in ("image_url", "audio_url", "video_url"):
                    media_tokens += 2000
        meta = (
            getattr(msg, "meta", None) or (msg.get("meta") if isinstance(msg, dict) else {}) or {}
        )
        params = meta.get("contentParams") or []
        if isinstance(params, dict):
            params = [params]
        if not isinstance(content, (list, tuple)):
            media_tokens += sum(
                2000
                for part in params
                if isinstance(part, dict)
                and part.get("type") in ("image_url", "audio_url", "video_url")
            )
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls is None and isinstance(msg, dict):
            tool_calls = msg.get("tool_calls") or msg.get("toolCalls")
        if isinstance(tool_calls, (list, tuple)):
            for tc in tool_calls:
                fn = tc.get("function") if isinstance(tc, dict) else getattr(tc, "function", None)
                if fn:
                    args = (
                        fn.get("arguments")
                        if isinstance(fn, dict)
                        else getattr(fn, "arguments", None)
                    )
                    if isinstance(args, str):
                        _add_text(args)
    return (total_chars + 3) // 4 + non_ascii_count + media_tokens


def should_auto_compact(
    token_count: int,
    max_context_size: int,
    *,
    trigger_ratio: float = 0.85,
    reserved_context_size: int = 50_000,
) -> bool:
    """Check if token_count triggers compaction (either condition)."""
    return max_context_size > 0 and (
        token_count >= max_context_size * trigger_ratio
        or (
            0 < reserved_context_size < max_context_size
            and token_count + reserved_context_size >= max_context_size
        )
    )


def estimate_context_tokens(
    messages: list[Any], active_tokens: int = 0, strategy: str = "measured+estimated"
) -> int:
    """Combine a persisted provider measurement with the unmeasured suffix.

    Compaction summaries invalidate earlier measurements. Measurements live on
    assistant rows, so resumption needs no process-local token ledger.
    """
    from coderai.soul.session.log import derive_messages
    from coderai.utils.common.usage import extract_usage_dict

    visible = derive_messages(messages)
    estimated = estimate_text_tokens(visible)
    if strategy == "estimated":
        return estimated
    has_summary = False
    summary_time = max(
        (
            getattr(message, "create_time", "")
            for message in visible
            if (getattr(message, "meta", None) or {}).get("isSummary")
            or (getattr(message, "meta", None) or {}).get("kind") == "compact/summary"
        ),
        default="",
    )
    measured = 0
    anchor = 0
    for index, message in enumerate(visible):
        meta = getattr(message, "meta", None) or {}
        if meta.get("isSummary") or meta.get("kind") == "compact/summary":
            has_summary = True
            measured = 0
        usage = meta.get("usage")
        if summary_time and getattr(message, "create_time", "") <= summary_time:
            continue
        if getattr(message, "role", None) == "assistant" and usage:
            tokens = extract_usage_dict(usage)["total_tokens"]
            if tokens > 0:
                measured = tokens
                anchor = index + 1
    if strategy == "measured":
        return measured or (0 if has_summary else max(0, active_tokens))
    if measured:
        return max(estimated, measured + estimate_text_tokens(visible[anchor:]))
    return estimated if has_summary else max(estimated, active_tokens)


def _tool_groups(messages: list[Any]) -> list[set[int]]:
    """Find complete exchanges, even when steering interleaves their results."""
    groups: list[set[int]] = []
    calls: dict[str, set[int]] = {}
    for index, message in enumerate(messages):
        if message.role == "assistant" and message.tool_calls:
            group = {index}
            groups.append(group)
            for call in message.tool_calls:
                call_id = call.get("id") if isinstance(call, dict) else getattr(call, "id", None)
                if call_id:
                    calls[call_id] = group
        elif message.role == "tool" and message.tool_call_id in calls:
            calls[message.tool_call_id].add(index)
    return groups


class CompactionEngine(abc.ABC):
    """Abstract seam for session compaction implementations."""

    @abc.abstractmethod
    async def compact_if_needed(
        self,
        session_id: str,
        trigger: str = "pressure",
    ) -> CompactionResult | None:
        """Conditionally compact if pressure/overflow thresholds are met."""
        ...

    @abc.abstractmethod
    async def compact_now(
        self,
        session_id: str,
        trigger: str = "manual",
    ) -> CompactionResult | None:
        """Explicitly compact the session (e.g. on user command or idle)."""
        ...

    @abc.abstractmethod
    async def compact_region(
        self,
        session_id: str,
        start_idx: int,
        end_idx: int,
        trigger: str = "pressure",
    ) -> CompactionResult | None:
        """Compact an explicit slice of messages into a summary."""
        ...


class BasicCompaction(CompactionEngine):
    """Default LLM-based compaction engine with tool-pairing protection."""

    def __init__(
        self,
        manager: SessionManager,
        pruner: ToolResultPruner | None = None,
    ) -> None:
        self.manager = manager
        self.pruner = pruner or ToolResultPruner(max_chars=2000)
        self._locks: dict[str, asyncio.Lock] = {}

    def _find_safe_region(
        self,
        messages: list[SessionMessage],
        preserve_ids: set[str] | None = None,
    ) -> tuple[int, int] | None:
        """Find a safe [start, end) index range that preserves tool pairing and preserved messages."""
        start = next((i for i, m in enumerate(messages) if m.role != "system"), -1)
        if start == -1:
            return None

        # Take roughly the older 2/3 of user/assistant/tool messages
        search_start = start + (len(messages) - start) * 2 // 3
        end = -1
        groups = _tool_groups(messages)
        for i in range(max(search_start, start), len(messages)):
            # Never cut immediately after an assistant tool_calls without its tool results
            # and never cut inside a tool result sequence
            if messages[i].role not in ("tool", "system") and not any(
                min(group) < i <= max(group) for group in groups
            ):
                end = i
                break

        if end == -1 or end <= start:
            return None

        return start, end

    async def compact_region(
        self,
        session_id: str,
        start_idx: int,
        end_idx: int,
        trigger: str = "pressure",
        preserve_ids: set[str] | None = None,
        custom_instruction: str | None = None,
    ) -> CompactionResult | None:
        async with self._locks.setdefault(session_id, asyncio.Lock()):
            return await self._compact_region(
                session_id, start_idx, end_idx, trigger, preserve_ids, custom_instruction
            )

    async def _compact_region(
        self,
        session_id: str,
        start_idx: int,
        end_idx: int,
        trigger: str,
        preserve_ids: set[str] | None,
        custom_instruction: str | None,
    ) -> CompactionResult | None:
        from coderai.prompt import get_compact_prompt
        from coderai.events import (
            make_compaction_start,
            make_compaction_summary,
            make_compaction_end,
        )
        from coderai.soul.session.log import derive_messages

        all_messages = self.manager.list_session_messages(session_id)
        messages = derive_messages(all_messages)
        if start_idx < 0 or end_idx > len(messages) or start_idx >= end_idx:
            return None

        target_slice = messages[start_idx:end_idx]
        if not target_slice:
            return None

        groups = _tool_groups(messages)
        selected = set(range(start_idx, end_idx))
        if any(group & selected and not group <= selected for group in groups):
            return None
        preserved = {
            i
            for i, message in enumerate(messages)
            if message.role == "system"
            or message.id in (preserve_ids or set())
            or (message.meta or {}).get("preserve") is True
            or (message.meta or {}).get("pinned") is True
        }
        for group in groups:
            assistant = messages[min(group)]
            expected = {
                call.get("id") if isinstance(call, dict) else getattr(call, "id", None)
                for call in assistant.tool_calls or []
            } - {None, ""}
            completed = {
                messages[index].tool_call_id for index in group if messages[index].role == "tool"
            }
            if group & preserved or expected - completed:
                preserved.update(group)
        replaced_ids = [m.id for i, m in enumerate(messages) if i in selected - preserved and m.id]
        if not replaced_ids:
            return None

        # Prune oversized tool result dumps from history before building prompt
        pruned_slice = self.pruner.prune_messages(target_slice)

        client_info = self.manager.create_openai_client()
        client = client_info.get("client")
        if client is None:
            return None

        compaction_id = f"cmp_{uuid.uuid4().hex[:10]}"
        model = self.manager.get_active_model()
        settings = self.manager.get_resolved_settings()
        thinking_enabled = bool(settings.get("thinkingEnabled"))

        prefix_messages = [m for m in messages[:start_idx] if m.role == "system"] + target_slice
        pruned_prefix = self.pruner.prune_messages(prefix_messages)
        converter = cast(
            CompactionMessageConverter | None,
            getattr(self.manager, "message_converter", None),
        )
        if converter is None or not callable(getattr(converter, "convert_session_messages", None)):
            from coderai.utils.common.message_converter import OpenAIMessageConverter

            converter = OpenAIMessageConverter()

        converted_prefix = converter.convert_session_messages(
            pruned_prefix,
            model=model,
            thinking_enabled=thinking_enabled,
        )

        _custom = custom_instruction
        if _custom:
            extra = f"\n\nAdditional focus instruction from user: {_custom}\nPay extra attention to this focus while keeping all sections."
        else:
            extra = ""
        compaction_directive = f"{COMPACTION_DIRECTIVE_TEMPLATE}{extra}"

        if converted_prefix:
            compaction_messages = list(converted_prefix) + [
                {"role": "user", "content": compaction_directive}
            ]
        else:
            prompt = get_compact_prompt(pruned_slice)
            compaction_messages = [{"role": "user", "content": prompt}]

        # AL-A3: Send compaction request without tools or with tool_choice="none"
        request: dict[str, Any] = {
            "model": model,
            "messages": compaction_messages,
            "tool_choice": "none",
        }

        # AL-A3: Use retrying completion helper
        response = await self.manager._create_completion_with_retry(
            session_id,
            client,
            request,
        )
        raw = (response.get("choices") or [{}])[0].get("message") or {}
        if (response.get("choices") or [{}])[0].get("finish_reason") in (
            "length",
            "max_tokens",
            "content_filter",
        ):
            return None
        raw_summary = str(raw.get("content") or "").strip()
        summary = re.sub(
            r"<analysis>[\s\S]*?</analysis>", "", raw_summary, flags=re.IGNORECASE
        ).strip()

        # AL-A3: Abort without committing if summary is empty
        if not summary:
            return None

        current = derive_messages(self.manager.list_session_messages(session_id))
        if current[:end_idx] != messages[:end_idx]:
            return None

        # Emit compaction/start with trigger metadata
        self.manager._append_event(
            session_id,
            make_compaction_start(
                self.manager._next_seq(session_id),
                compaction_id,
                {"start": start_idx, "end": end_idx},
                trigger=trigger,
            ),
        )

        tokens = estimate_text_tokens(target_slice)
        replaced_id_set = set(replaced_ids)

        shadowed_seqs: list[int] = []
        try:
            rows = self.manager.session_store.read_rows(session_id)
        except Exception:
            rows = []
        for row in rows:
            data = row.get("data")
            row_data = data if isinstance(data, dict) else {}
            row_id = row.get("id") or row_data.get("id")
            if not row_id and row.get("type") == "tool/result":
                row_id = f"{session_id}:event:{row.get('seq')}"
            if row_id in replaced_id_set or row_data.get("compactionId") in replaced_id_set:
                seq = row.get("seq")
                if isinstance(seq, int) and seq not in shadowed_seqs:
                    shadowed_seqs.append(seq)

        # Emit compaction/summary event
        summary_seq = self.manager._next_seq(session_id)
        self.manager._append_event(
            session_id,
            make_compaction_summary(
                summary_seq,
                compaction_id,
                summary,
                shadowed_seqs=shadowed_seqs,
                shadowed_ids=replaced_ids,
            ),
        )

        # Emit compaction/end event
        self.manager._append_event(
            session_id,
            make_compaction_end(
                self.manager._next_seq(session_id),
                compaction_id,
                tokens,
            ),
        )

        return CompactionResult(
            compaction_id=compaction_id,
            summary=summary,
            shadowed_range={"start": start_idx, "end": end_idx},
            shadowed_ids=replaced_ids,
            shadowed_seqs=shadowed_seqs,
            shadowed_token_count=tokens,
            tokens_after=estimate_context_tokens(self.manager.list_session_messages(session_id)),
        )

    async def compact_now(
        self,
        session_id: str,
        trigger: str = "manual",
        preserve_ids: set[str] | None = None,
        custom_instruction: str | None = None,
    ) -> CompactionResult | None:
        from coderai.soul.session.log import derive_messages

        async with self._locks.setdefault(session_id, asyncio.Lock()):
            messages = self.manager.list_session_messages(session_id)
            derived = derive_messages(messages)
            region = self._find_safe_region(derived, preserve_ids=preserve_ids)
            if not region:
                return None
            return await self._compact_region(
                session_id, region[0], region[1], trigger, preserve_ids, custom_instruction
            )

    async def compact_if_needed(
        self,
        session_id: str,
        trigger: str = "pressure",
        preserve_ids: set[str] | None = None,
    ) -> CompactionResult | None:
        from coderai.prompt import calculate_context_budget

        entry = self.manager._get_entry(session_id) or {}
        active_tokens = int(entry.get("activeTokens", 0) or 0)
        messages = self.manager.list_session_messages(session_id)
        model = self.manager.get_active_model()
        settings = self.manager.get_resolved_settings()
        budget = calculate_context_budget(model, context_limit=settings.get("contextWindow"))
        limit = budget["context_limit"]
        pressure_ratio = float(settings.get("compactionTriggerRatio") or 0.85)
        token_count = estimate_context_tokens(
            messages, active_tokens, settings.get("tokenCountingStrategy", "measured+estimated")
        )
        reserved_size = int(settings.get("reservedContextSize", 50_000))
        auto_window = settings.get("autoCompactWindow")

        evaluated_trigger = evaluate_compaction_trigger(
            token_count,
            limit,
            pressure_ratio=pressure_ratio,
            overflow_ratio=0.95,
            reserved_context_size=reserved_size,
        )
        if auto_window and token_count > auto_window:
            evaluated_trigger = evaluated_trigger or "pressure"

        if (
            trigger == "force"
            or evaluated_trigger == trigger
            or (trigger == "pressure" and evaluated_trigger in ("pressure", "overflow"))
            or (trigger == "overflow" and evaluated_trigger == "overflow")
        ):
            return await self.compact_now(
                session_id,
                trigger=evaluated_trigger or trigger,
                preserve_ids=preserve_ids,
            )
        return None
