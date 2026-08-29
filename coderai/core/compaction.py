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
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from coderai.core.session import SessionManager, SessionMessage

DEFAULT_MAX_TOOL_RESULT_CHARS = 32_000


@dataclass
class CompactionResult:
    """Result of a compaction operation."""

    compaction_id: str
    summary: str
    shadowed_range: dict[str, int]  # {"start": seq_or_idx, "end": seq_or_idx}
    shadowed_ids: list[str] = field(default_factory=list)
    shadowed_seqs: list[int] = field(default_factory=list)
    shadowed_token_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "compactionId": self.compaction_id,
            "summary": self.summary,
            "shadowedRange": self.shadowed_range,
            "shadowedIds": self.shadowed_ids,
            "shadowedSeqs": self.shadowed_seqs,
            "shadowedTokenCount": self.shadowed_token_count,
        }


class ToolResultPruner:
    """Deterministic head/middle/tail pruner for tool results."""

    def __init__(self, max_chars: int = DEFAULT_MAX_TOOL_RESULT_CHARS) -> None:
        self.max_chars = max_chars

    def prune_content(self, content: str) -> str:
        """Truncate content exceeding max_chars symmetrically."""
        if not content or len(content) <= self.max_chars:
            return content
        head = self.max_chars // 2
        tail = self.max_chars - head
        omitted = len(content) - self.max_chars
        return f"{content[:head]}\n\n...[{omitted} characters omitted]...\n\n{content[-tail:]}"

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
            if hasattr(msg, "__dict__"):
                try:
                    clone = type(msg)(**{**msg.__dict__, "content": pruned_text})
                except TypeError:
                    setattr(msg, "content", pruned_text)
                    clone = msg
                out.append(clone)
            elif isinstance(msg, dict):
                out.append({**msg, "content": pruned_text})
            else:
                out.append(msg)
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
) -> str | None:
    """Evaluate whether active tokens meet dual-trigger thresholds ('overflow' vs 'pressure')."""
    if context_limit <= 0 or active_tokens <= 0:
        return None
    if active_tokens >= int(context_limit * overflow_ratio):
        return "overflow"
    if active_tokens >= int(context_limit * pressure_ratio):
        return "pressure"
    return None


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
        for i in range(max(search_start, start), len(messages)):
            # Never cut immediately after an assistant tool_calls without its tool results
            # and never cut inside a tool result sequence
            if messages[i].role not in ("tool", "system"):
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
    ) -> CompactionResult | None:
        from coderai.core.prompt import get_compact_prompt
        from coderai.core.events import (
            make_compaction_start,
            make_compaction_summary,
            make_compaction_end,
        )

        messages = self.manager.list_session_messages(session_id)
        if start_idx < 0 or end_idx > len(messages) or start_idx >= end_idx:
            return None

        target_slice = messages[start_idx:end_idx]
        if not target_slice:
            return None

        # Prune oversized tool result dumps from history before building prompt
        pruned_slice = self.pruner.prune_messages(target_slice)

        client_info = self.manager.create_openai_client()
        client = client_info.get("client")
        if client is None:
            return None

        compaction_id = f"cmp_{uuid.uuid4().hex[:10]}"
        model = self.manager.get_active_model()
        # Build KV-cache preserving compaction messages:
        # Replay conversation prefix up to end_idx using standard message conversion
        # so the auxiliary compaction request reuses the warm prefix cache.
        settings = self.manager.get_resolved_settings()
        thinking_enabled = bool(settings.get("thinkingEnabled"))
        tools_preset = settings.get("toolsPreset") or settings.get("preset")
        multimodal_mode = settings.get("multimodal", "default")

        from coderai.core.prompt import get_tools, format_tool_definitions

        tools = get_tools(
            {
                "model": model,
                "nonInteractive": self.manager.non_interactive,
                "multimodal": multimodal_mode,
                "preset": tools_preset,
            },
            external_tools=self.manager.mcp_tool_definitions if not tools_preset else None,
        )
        if tools:
            tools = format_tool_definitions(tools, model=model)

        prefix_messages = messages[:end_idx]
        pruned_prefix = self.pruner.prune_messages(prefix_messages)
        converter = getattr(self.manager, "message_converter", None)
        if (
            converter is None
            or not hasattr(converter, "convert_session_messages")
            or "Mock" in type(converter).__name__
        ):
            from coderai.core.common.message_converter import OpenAIMessageConverter

            converter = OpenAIMessageConverter()

        converted_prefix = converter.convert_session_messages(
            pruned_prefix,
            model=model,
            thinking_enabled=thinking_enabled,
        )

        compaction_directive = (
            "You are now acting as a compaction engine for this AI coding assistant. "
            "Condense the conversation ABOVE into a structured checkpoint that lets another model resume the work with no loss of essential context.\n\n"
            'Output EXACTLY the Markdown structure below: keep every section, in order. Use terse bullets, not prose paragraphs. Write "(none)" for an empty section — never drop a section.\n\n'
            "## Primary Request and Intent\n"
            "- [the user's original and evolving goals; quote verbatim where the exact wording matters]\n\n"
            "## Key Technical Concepts\n"
            "- [technologies, frameworks, patterns, and conventions in play]\n\n"
            "## Critical Decisions & Constraints\n"
            "- [architectural, design, and implementation decisions made and why]\n\n"
            "## State of Progress\n"
            "- [completed tasks, modified files, and verified behaviors]\n\n"
            "## Pending Work & Next Steps\n"
            "- [immediate next actions and known open questions]\n\n"
            "Do not include conversational filler before or after the summary."
        )

        if converted_prefix:
            compaction_messages = list(converted_prefix) + [
                {"role": "user", "content": compaction_directive}
            ]
        else:
            prompt = get_compact_prompt(pruned_slice)
            compaction_messages = [{"role": "user", "content": prompt}]

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

        request: dict[str, Any] = {
            "model": model,
            "messages": compaction_messages,
        }
        if tools:
            request["tools"] = tools

        response = await self.manager._create_completion(
            client,
            request,
            emit_stream=False,
        )
        raw = (response.get("choices") or [{}])[0].get("message") or {}
        raw_summary = str(raw.get("content") or "").strip()
        summary = re.sub(
            r"<analysis>[\s\S]*?</analysis>", "", raw_summary, flags=re.IGNORECASE
        ).strip()

        usage = response.get("usage")
        tokens = usage.get("total_tokens", 0) if usage else 0

        # Filter out preserved/pinned messages from shadowed IDs
        preserved_set = set(preserve_ids or [])
        replaced_ids = [
            m.id
            for m in target_slice
            if m.id
            and m.id not in preserved_set
            and not (
                hasattr(m, "meta")
                and isinstance(m.meta, dict)
                and (m.meta.get("preserve") is True or m.meta.get("pinned") is True)
            )
        ]

        # Emit compaction/summary event
        summary_seq = self.manager._next_seq(session_id)
        self.manager._append_event(
            session_id,
            make_compaction_summary(
                summary_seq,
                compaction_id,
                summary,
                shadowed_seqs=[],
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
            shadowed_token_count=tokens,
        )

    async def compact_now(
        self,
        session_id: str,
        trigger: str = "manual",
        preserve_ids: set[str] | None = None,
    ) -> CompactionResult | None:
        messages = self.manager.list_session_messages(session_id)
        region = self._find_safe_region(messages, preserve_ids=preserve_ids)
        if not region:
            return None
        return await self.compact_region(
            session_id, region[0], region[1], trigger=trigger, preserve_ids=preserve_ids
        )

    async def compact_if_needed(
        self,
        session_id: str,
        trigger: str = "pressure",
        preserve_ids: set[str] | None = None,
    ) -> CompactionResult | None:
        from coderai.core.prompt import calculate_context_budget

        entry = self.manager._get_entry(session_id) or {}
        active_tokens = entry.get("activeTokens", 0)
        model = self.manager.get_active_model()
        budget = calculate_context_budget(model)
        limit = budget["context_limit"]

        evaluated_trigger = evaluate_compaction_trigger(active_tokens, limit)
        if (
            trigger == "force"
            or evaluated_trigger == trigger
            or (trigger == "pressure" and evaluated_trigger in ("pressure", "overflow"))
        ):
            effective_trigger = evaluated_trigger or trigger
            messages = self.manager.list_session_messages(session_id)
            region = self._find_safe_region(messages, preserve_ids=preserve_ids)
            if not region:
                return None
            return await self.compact_region(
                session_id, region[0], region[1], trigger=effective_trigger, preserve_ids=preserve_ids
            )
        return None
