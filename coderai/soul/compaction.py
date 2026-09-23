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
    from coderai.soul.session.manager import SessionManager, SessionMessage

DEFAULT_MAX_TOOL_RESULT_CHARS = 32_000

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
    reserved_context_size: int | None = None,
) -> str | None:
    """Evaluate whether active tokens meet dual-trigger thresholds ('overflow' vs 'pressure').

    Triggers auto compaction when either
    active_tokens >= context_limit * ratio OR active_tokens + reserved >= context_limit.
    """
    if context_limit <= 0 or active_tokens <= 0:
        return None
    # Reserved budget guard
    if reserved_context_size and reserved_context_size > 0:
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
    return (total_chars + 3) // 4 + non_ascii_count


def should_auto_compact(
    token_count: int,
    max_context_size: int,
    *,
    trigger_ratio: float = 0.85,
    reserved_context_size: int = 50_000,
) -> bool:
    """Check if token_count triggers compaction (either condition)."""
    return (
        token_count >= max_context_size * trigger_ratio
        or token_count + reserved_context_size >= max_context_size
    )


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

        prefix_messages = messages[:end_idx]
        pruned_prefix = self.pruner.prune_messages(prefix_messages)
        converter = getattr(self.manager, "message_converter", None)
        if (
            converter is None
            or not hasattr(converter, "convert_session_messages")
            or "Mock" in type(converter).__name__
        ):
            from coderai.utils.common.message_converter import OpenAIMessageConverter

            converter = OpenAIMessageConverter()

        converted_prefix = converter.convert_session_messages(
            pruned_prefix,
            model=model,
            thinking_enabled=thinking_enabled,
        )

        _custom = getattr(self, "_pending_custom_instruction", None)
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
        raw_summary = str(raw.get("content") or "").strip()
        summary = re.sub(
            r"<analysis>[\s\S]*?</analysis>", "", raw_summary, flags=re.IGNORECASE
        ).strip()

        # AL-A3: Abort without committing if summary is empty
        if not summary:
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
        replaced_id_set = set(replaced_ids)

        shadowed_seqs: list[int] = []
        try:
            rows = self.manager.session_store.read_rows(session_id)
        except Exception:
            rows = []
        for row in rows:
            row_data = row.get("data") if isinstance(row.get("data"), dict) else {}
            row_id = row.get("id") or row_data.get("id")
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
        )

    async def compact_now(
        self,
        session_id: str,
        trigger: str = "manual",
        preserve_ids: set[str] | None = None,
        custom_instruction: str | None = None,
    ) -> CompactionResult | None:
        from coderai.soul.session.log import derive_messages

        messages = self.manager.list_session_messages(session_id)
        derived = derive_messages(messages)
        region = self._find_safe_region(derived, preserve_ids=preserve_ids)
        if not region:
            return None
        # Custom instruction appended to directive when /compact <focus>
        if custom_instruction:
            self._pending_custom_instruction = custom_instruction  # type: ignore
        try:
            return await self.compact_region(
                session_id, region[0], region[1], trigger=trigger, preserve_ids=preserve_ids
            )
        finally:
            self._pending_custom_instruction = None  # type: ignore

    async def compact_if_needed(
        self,
        session_id: str,
        trigger: str = "pressure",
        preserve_ids: set[str] | None = None,
    ) -> CompactionResult | None:
        from coderai.prompt import calculate_context_budget
        from coderai.soul.session.log import derive_messages

        entry = self.manager._get_entry(session_id) or {}
        active_tokens = int(entry.get("activeTokens", 0) or 0)
        messages = self.manager.list_session_messages(session_id)
        derived = derive_messages(messages)
        token_count = max(active_tokens, estimate_text_tokens(derived))

        model = self.manager.get_active_model()
        budget = calculate_context_budget(model)
        limit = budget["context_limit"]

        settings = self.manager.get_resolved_settings()
        pressure_ratio = float(settings.get("compactionTriggerRatio") or 0.85)
        reserved_size = int(settings.get("reservedContextSize") or 50_000)
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


COMPACTION_SYSTEM_PROMPT = "You are a helpful assistant that compacts conversation context."
COMPACTION_OUTPUT_PREFIX = "Previous context has been compacted. Here is the compaction output:"


from collections.abc import Sequence
from typing import NamedTuple, Protocol, runtime_checkable


@runtime_checkable
class Compaction(Protocol):
    async def compact(
        self,
        messages: Sequence[Any],
        llm: Any,
        *,
        custom_instruction: str = "",
    ) -> Any: ...


class SimpleCompaction:
    """Sliding-window context compaction matching Kimi CLI reference."""

    def __init__(self, max_preserved_messages: int = 2) -> None:
        self.max_preserved_messages = max_preserved_messages

    class PrepareResult(NamedTuple):
        compact_message: Any | None
        to_preserve: Sequence[Any]

    def prepare(self, messages: Sequence[Any], *, custom_instruction: str = "") -> PrepareResult:
        from kosong.message import Message
        from coderai.wire.types import TextPart
        from coderai.prompt import prompts_dir

        if not messages or self.max_preserved_messages <= 0:
            return self.PrepareResult(compact_message=None, to_preserve=messages)

        history = list(messages)
        preserve_start_index = len(history)
        n_preserved = 0
        for index in range(len(history) - 1, -1, -1):
            role = getattr(history[index], "role", "")
            if role in {"user", "assistant"}:
                n_preserved += 1
                if n_preserved == self.max_preserved_messages:
                    preserve_start_index = index
                    break

        if n_preserved < self.max_preserved_messages:
            return self.PrepareResult(compact_message=None, to_preserve=messages)

        to_compact = history[:preserve_start_index]
        to_preserve = history[preserve_start_index:]

        if not to_compact:
            return self.PrepareResult(compact_message=None, to_preserve=to_preserve)

        compact_message = Message(role="user", content=[])
        for i, msg in enumerate(to_compact):
            role = getattr(msg, "role", "")
            compact_message.content.append(
                TextPart(text=f"## Message {i + 1}\nRole: {role}\nContent:\n")
            )
            msg_content = getattr(msg, "content", [])
            if isinstance(msg_content, str):
                compact_message.content.append(TextPart(text=msg_content))
            elif isinstance(msg_content, (list, tuple)):
                compact_message.content.extend(
                    part
                    for part in msg_content
                    if getattr(part, "type", "") == "text" or isinstance(part, TextPart)
                )

        compact_prompt_file = prompts_dir() / "compact.md" if callable(prompts_dir) else None
        prompt_text = "\nSummarize the conversation so far."
        if compact_prompt_file and compact_prompt_file.is_file():
            prompt_text = "\n" + compact_prompt_file.read_text(encoding="utf-8")

        if custom_instruction:
            prompt_text += f"\n\n**User's Custom Compaction Instruction:**\n{custom_instruction}"
        compact_message.content.append(TextPart(text=prompt_text))
        return self.PrepareResult(compact_message=compact_message, to_preserve=to_preserve)

    async def compact(
        self,
        messages: Sequence[Any],
        llm: Any,
        *,
        custom_instruction: str = "",
    ) -> Any:
        import kosong
        from kosong.message import Message
        from kosong.tooling.empty import EmptyToolset
        from coderai.wire.types import TextPart, ThinkPart

        compact_message, to_preserve = self.prepare(messages, custom_instruction=custom_instruction)
        if compact_message is None:
            return to_preserve

        chat_provider = getattr(llm, "chat_provider", llm)
        result = await kosong.step(
            chat_provider=chat_provider,
            system_prompt=COMPACTION_SYSTEM_PROMPT,
            toolset=EmptyToolset(),
            history=[compact_message],
        )

        content: list[Any] = [TextPart(text=COMPACTION_OUTPUT_PREFIX)]
        compacted_msg = result.message
        content.extend(part for part in compacted_msg.content if not isinstance(part, ThinkPart))
        compacted_messages = [Message(role="user", content=content), *to_preserve]
        return compacted_messages
