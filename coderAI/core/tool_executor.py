"""Tool execution and orchestration for the CoderAI agent.

Handles batch parallelism, read-only vs mutating limits, hook execution,
and UI confirmation.
"""

import asyncio
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from coderAI.core.agent_tracker import AgentStatus
from coderAI.core.loop_guard import (
    DOOM_LOOP_HARD_THRESHOLD as DOOM_LOOP_HARD_THRESHOLD,  # re-exported for tests
    LoopGuard,
    doom_message,
)
from coderAI.core.services import get_services, services_scope
from coderAI.core.tool_routing import (
    call_mcp_tool_by_function_name as call_mcp_tool_by_function_name,  # compatibility patch point
    is_mcp_function_name,
    coerce_tool_arguments,
)
from coderAI.types.provenance import Provenance, wrap_untrusted_output
from coderAI.types.tool_error_codes import ToolErrorCode
from coderAI.core.turn import TurnContext
from coderAI.core.ports import AgentRuntime, RuntimeView
from coderAI.core.tool_batch_scheduler import BatchScheduler
from coderAI.core.tool_confirmation import ConfirmationGate
from coderAI.core.tool_transaction import TransactionBracket

logger = logging.getLogger(__name__)


# Cap concurrent read-only tools — auto-scaled by CPU count so large
# machines can fan out grep/read_file, small machines stay safe.
# Throughput tuning: 32-64 concurrent reads saturates NVMe + network
# without thread-pool starvation; 6× cores gives larger headroom on 8+
# core hosts where concurrent grep/read_file fan-out is highly profitable.
def _default_ro_cap() -> int:
    try:
        import os

        cpus = os.cpu_count() or 4
        return max(32, min(64, cpus * 6))
    except Exception:
        return 32


MAX_CONCURRENT_READ_ONLY = _default_ro_cap()

DEFAULT_TOOL_TIMEOUT_SECONDS = 120.0

# Ceiling on the exponential backoff between transient-failure tool retries.
TOOL_RETRY_DELAY_CAP_SECONDS = 10.0


def resolve_tool_timeout(tool: Any, tool_name: str, arguments: Any) -> float:
    """Effective outer wall-clock cap for one tool call.

    Precedence (first hit wins):

    1. ``tool.resolve_timeout(arguments)`` — argument-derived cap (a tool with
       its own ``timeout`` argument returns it clamped + margin, so the outer
       ``wait_for`` can't fire before the tool's own subprocess cleanup);
    2. ``config.tool_timeout_overrides[tool_name]`` — per-tool config override;
    3. ``tool.timeout`` class attribute;
    4. ``config.tool_timeout_seconds`` — only when explicitly set (config
       file / env / project overlay), so the pydantic default doesn't shadow
       the monkeypatchable module default below;
    5. ``DEFAULT_TOOL_TIMEOUT_SECONDS`` (read live so tests can patch it).

    All access is defensive (``getattr`` / try-except): tests exercise the
    executor with ``SimpleNamespace`` tools and mock agents, and a broken
    ``resolve_timeout`` must degrade to the next level, never sink the call.
    """
    resolver = getattr(tool, "resolve_timeout", None)
    if callable(resolver):
        try:
            resolved = resolver(arguments if isinstance(arguments, dict) else {})
            if resolved is not None:
                return float(resolved)
        except Exception:
            logger.debug("resolve_timeout failed for %s; falling back", tool_name, exc_info=True)

    config: Any = None
    try:
        config = get_services().config
    except Exception:
        config = None

    if config is not None:
        overrides = getattr(config, "tool_timeout_overrides", None)
        if isinstance(overrides, dict):
            override = overrides.get(tool_name)
            if override is not None:
                try:
                    return float(override)
                except (TypeError, ValueError):
                    pass

    tool_timeout = getattr(tool, "timeout", None)
    if tool_timeout:
        try:
            return float(tool_timeout)
        except (TypeError, ValueError):
            pass

    if config is not None and "tool_timeout_seconds" in getattr(config, "model_fields_set", ()):
        try:
            value = float(config.tool_timeout_seconds)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass

    return DEFAULT_TOOL_TIMEOUT_SECONDS


# Cap concurrent read-only sub-agent delegations. Raised from 4 → 8 and
# auto-scaled: each sub-agent is a full LLM session, but modern hosts
# handle 6-8 concurrent fetch/research agents trivially (mirrors Claude
# Code's 3-5× fan-out win). The semaphore is still bounded to avoid OOM.
def _default_ro_subagent_cap() -> int:
    try:
        import os

        cpus = os.cpu_count() or 4
        return max(6, min(10, cpus * 2))
    except Exception:
        return 8


MAX_CONCURRENT_READ_ONLY_SUBAGENTS = _default_ro_subagent_cap()

DEFAULT_MAX_CONCURRENT_MUTATING_SUBAGENTS = 4

# Maximum number of entries in the preview file cache. Beyond this limit, the
# least-recently-used entry is evicted to bound memory usage.
PREVIEW_FILE_CACHE_MAX_ENTRIES = 50

# Maximum combined size (bytes) of cached file contents. When exceeded, LRU
# entries are dropped until the total is within the limit.
PREVIEW_FILE_CACHE_MAX_BYTES = 5 * 1024 * 1024  # 5 MB


class BatchStatus(Enum):
    """Outcome of running one batch of tool calls (Phase 2.1).

    Replaces the old ``Tuple[bool, Optional[Dict]]`` with sentinel keys
    (``{"retry": True}`` / ``{"_denied": ...}`` / ``{"_doom_loop_stop": ...}``)
    that ``ExecutionLoop`` had to reverse-engineer with an if-cascade.
    """

    OK = "ok"  # tools ran; at least one succeeded — continue the loop.
    RETRY = "retry"  # all failed (or unparsable) — feed errors back to the LLM.
    DENIED = "denied"  # one or more tools were denied by the user.
    DOOM_LOOP = "doom_loop"  # identical call repeated past the hard threshold.


@dataclass
class ToolBatchOutcome:
    """Typed result of :meth:`ToolExecutor.orchestrate_tool_calls`."""

    status: BatchStatus
    denied_tools: list[str] = field(default_factory=list)
    doom_tool: Optional[str] = None
    doom_count: int = 0


def _extract_vision_images(
    res: Any,
) -> tuple[Any, Optional[list[dict[str, Any]]]]:
    """Split a vision tool result into a lightweight text dict + image blocks.

    Tools like ``read_image`` return ``{"_vision": True, "image_data": <b64>,
    "mime_type": ...}``. The base64 payload must NOT go through result
    summarization (it would be truncated and corrupted) or be stringified into
    the text content (huge + useless to the model). This pulls the image out so
    it can be carried as a structured ``tool_images`` block, leaving a small
    text dict behind. Returns ``(clean_result, images)`` where ``images`` is
    ``None`` when the result carries no usable image.
    """
    if not isinstance(res, dict) or not res.get("_vision"):
        return res, None
    data = res.get("image_data")
    mime = res.get("mime_type")
    if not (isinstance(data, str) and data and isinstance(mime, str) and mime):
        return res, None
    images = [{"mime_type": mime, "data": data}]
    clean = {k: v for k, v in res.items() if k != "image_data"}
    clean["image_attached"] = True
    return clean, images


class ToolExecutor(ConfirmationGate, TransactionBracket, BatchScheduler):
    agent: Any
    loop_guard: LoopGuard
    _turn: TurnContext
    _ro_semaphore: asyncio.Semaphore
    _subagent_ro_semaphore: asyncio.Semaphore
    _subagent_mut_semaphore: asyncio.Semaphore
    _confirm_lock: asyncio.Lock
    _preview_file_cache: "OrderedDict[str, tuple[float, str]]"

    def __init__(self, agent: AgentRuntime, loop_guard: Optional[LoopGuard] = None) -> None:
        self.agent = agent
        self.runtime = RuntimeView(agent)
        # Per-turn state shared with ``ExecutionLoop`` (Phase 4.1). ``run()``
        # passes its ``TurnContext`` into ``orchestrate_tool_calls``; a standalone
        # executor (tests) keeps this default so the egress-gate taint persists
        # across successive batches on the same instance.
        self._turn = TurnContext()
        # One guard per turn owns fingerprinting, repeat counters, cached-repeat
        # decisions, and the doom-loop thresholds (Phase 2.2). ``ExecutionLoop``
        # creates it and shares the same instance so the in-batch and
        # cross-iteration paths agree. A standalone executor (tests) gets its own.
        self.loop_guard = loop_guard if loop_guard is not None else LoopGuard()
        self._ro_semaphore = asyncio.Semaphore(MAX_CONCURRENT_READ_ONLY)
        self._subagent_ro_semaphore = asyncio.Semaphore(MAX_CONCURRENT_READ_ONLY_SUBAGENTS)
        mut_cap = self._mutating_subagent_cap()
        self._subagent_mut_semaphore = asyncio.Semaphore(mut_cap)
        self._confirm_lock = asyncio.Lock()
        self._preview_file_cache: "OrderedDict[str, tuple[float, str]]" = OrderedDict()
        # Once a mutation has run in this turn, earlier cached reads may be
        # stale. Consecutive reads can still dedupe within their current phase.
        self._mutation_seen = False

    def _result_provenance(self, tool_name: str) -> str:
        """Taint label for *tool_name*'s results (Phase 3.1).

        Real tools declare ``result_provenance``; MCP proxy calls (no local Tool
        object) are always ``UNTRUSTED_EXTERNAL`` — a third-party server's output
        must never carry system authority (confused-deputy, Phase 7.3).
        """
        tool = self.agent.tools.get(tool_name)
        if tool is not None:
            return str(getattr(tool, "result_provenance", Provenance.TRUSTED))
        if is_mcp_function_name(tool_name):
            return Provenance.UNTRUSTED_EXTERNAL
        return Provenance.TRUSTED

    def _mark_turn_untrusted(self, *, from_mcp: bool = False) -> None:
        """Record that this user turn has ingested untrusted external content.

        Arms the egress gate (:meth:`_turn_has_untrusted`). When the content came
        from an MCP server, also arms the stronger mutating-local gate
        (:meth:`_turn_has_untrusted_mcp`, Phase 7.3). The taint lives on the shared
        :class:`TurnContext`, which is fresh per user message.
        """
        self._turn.ingested_untrusted = True
        if from_mcp:
            self._turn.ingested_untrusted_mcp = True

    def _turn_has_untrusted(self) -> bool:
        return self._turn.ingested_untrusted

    def _turn_has_untrusted_mcp(self) -> bool:
        return self._turn.ingested_untrusted_mcp

    @staticmethod
    def _untrusted_source(pc: dict[str, Any]) -> str:
        """Short ``source`` label for the untrusted-output fence.

        Tool name, plus the fetch target (url/query) when available so a reviewer
        can see where the content came from. Sanitized by ``wrap_untrusted_output``.
        """
        name = pc.get("tool_name", "unknown")
        args = pc.get("arguments") or {}
        target = None
        if isinstance(args, dict):
            target = args.get("url") or args.get("query")
        if isinstance(target, str) and target.strip():
            return f"{name}:{target}"
        return str(name)

    @staticmethod
    def _dedupe_safe(tool: Any) -> bool:
        """Whether an identical call may reuse a prior result."""
        if tool is None:
            return False
        declared = getattr(tool, "dedupe_safe", None)
        if declared is not None:
            return bool(declared)
        return bool(getattr(tool, "is_read_only", False))

    @staticmethod
    def _idempotent(tool: Any) -> bool:
        """Whether retrying an identical call is safe."""
        if tool is None:
            return False
        declared = getattr(tool, "idempotent", None)
        if declared is not None:
            return bool(declared)
        return bool(getattr(tool, "is_read_only", False))

    async def orchestrate_tool_calls(
        self,
        tool_calls: list,
        messages: list[dict[str, Any]],
        user_message: str,
        hooks_data: Optional[dict[str, Any]],
        hooks_manager: Any,
        turn: Optional[TurnContext] = None,
    ) -> ToolBatchOutcome:
        # Adopt the loop-owned per-turn state (Phase 4.1) so the egress-gate
        # taint and reply state live in one object. A direct/test call without a
        # turn keeps the executor's own default ``TurnContext``.
        if turn is not None:
            if turn is not self._turn:
                self._mutation_seen = False
            self._turn = turn
        # Bind the owning agent's effective config (project overrides included)
        # and its session-pinned workspace-trust decision for the duration of
        # the batch. Recovery state is selected from each tool call's immutable
        # RunContext; intentionally shared services (tracker/MCP) still inherit.
        with services_scope(
            inherit=True,
            config=self.runtime.config,
            workspace_trusted=self.runtime.workspace_trusted,
            context_controller=self.runtime.context_controller,
        ):
            return await self._orchestrate_tool_calls(
                tool_calls, messages, user_message, hooks_data, hooks_manager
            )

    async def _orchestrate_tool_calls(
        self,
        tool_calls: list,
        messages: list[dict[str, Any]],
        user_message: str,
        hooks_data: Optional[dict[str, Any]],
        hooks_manager: Any,
    ) -> ToolBatchOutcome:
        parsed_calls = []
        parse_failures = 0
        for tc in tool_calls:
            tool_id = tc.get("id", "")
            func = tc.get("function", {}) or {}
            name = func.get("name", "") or ""
            raw_args = func.get("arguments")
            args, arg_err = coerce_tool_arguments(raw_args)
            if arg_err is not None:
                parse_failures += 1
                parsed_calls.append(
                    {
                        "tool_id": tool_id,
                        "tool_name": name,
                        "arguments": None,
                        "parse_error": arg_err,
                    }
                )
            else:
                parsed_calls.append(
                    {"tool_id": tool_id, "tool_name": name, "arguments": args, "parse_error": None}
                )

        if parse_failures == len(parsed_calls):
            # All tools failed to parse — record the synthetic tool replies and
            # ask the loop for another LLM round. The loop's
            # ``consecutive_errors`` counter terminates if this keeps happening.
            for pc in parsed_calls:
                if self._turn.objective_state is not None:
                    self._turn.objective_state.record_tool_result(
                        pc["tool_name"],
                        None,
                        {
                            "success": False,
                            "error": pc["parse_error"],
                            "error_code": ToolErrorCode.PARSE_ERROR,
                        },
                        self.agent.tools.get(pc["tool_name"]),
                    )
                self.agent.session.add_message(
                    "tool",
                    json.dumps(
                        {
                            "success": False,
                            "error": pc["parse_error"],
                            "error_code": ToolErrorCode.PARSE_ERROR,
                        }
                    ),
                    tool_call_id=pc["tool_id"],
                    name=pc["tool_name"],
                )

            messages.clear()
            messages.extend(self.agent.session.get_messages_for_api())
            return ToolBatchOutcome(BatchStatus.RETRY)

        if self.agent.tracker_info:
            self.agent.tracker_update(
                status=AgentStatus.TOOL_CALL,
                current_tool=", ".join(pc["tool_name"] for pc in parsed_calls if pc["arguments"]),
            )

        for pc in parsed_calls:
            if pc["parse_error"] is not None:
                get_services().events.emit(
                    "tool_error", tool_name=pc["tool_name"], error=pc["parse_error"]
                )
            elif pc["arguments"] is not None:
                get_services().events.emit(
                    "tool_call",
                    tool_name=pc["tool_name"],
                    arguments=pc["arguments"],
                    tool_id=pc["tool_id"],
                )

        dup_results: dict[int, dict[str, Any]] = {}
        batch_seen: dict[str, int] = {}
        to_run_indices: list[int] = []
        mutation_before = self._mutation_seen
        for idx, pc in enumerate(parsed_calls):
            if pc["parse_error"] is not None or pc["arguments"] is None:
                to_run_indices.append(idx)
                continue
            fp = self.loop_guard.fingerprint(pc["tool_name"], pc["arguments"])
            pc["_fp"] = fp
            tool = self.agent.tools.get(pc["tool_name"])
            dedupe_safe = self._dedupe_safe(tool)
            is_read_only = bool(tool and getattr(tool, "is_read_only", False))
            if not is_read_only:
                # A read result cannot be reused across a mutation barrier.
                batch_seen.clear()

            if dedupe_safe and fp in batch_seen:
                dup_results[idx] = {
                    "_dup_of_batch_index": batch_seen[fp],
                    "_warning": (
                        f"Duplicate call to '{pc['tool_name']}' in the same batch — "
                        "result reused from the first call. Avoid emitting identical "
                        "parallel tool calls."
                    ),
                }
                continue

            prior_count = self.loop_guard.prior_count(fp)
            repeat = self.loop_guard.cached_repeat(
                pc["tool_name"], dedupe_safe and not mutation_before, fp
            )
            if repeat is not None:
                cached, repeated_count = repeat
                pc["_cached_repeat_count"] = repeated_count
                if pc["tool_name"] == "delegate_task":
                    cached["_warning"] = (
                        f"This is call #{repeated_count} to 'delegate_task' with identical "
                        "arguments — returning the cached report. Do not re-delegate the same task."
                    )
                else:
                    cached["_warning"] = (
                        f"This is call #{repeated_count} to '{pc['tool_name']}' with identical "
                        "arguments — returning the cached result. Stop repeating the same read; "
                        "either work with the data you already have or try a different approach."
                    )
                dup_results[idx] = cached
                get_services().events.emit(
                    "agent_warning",
                    message=(
                        f"Skipping duplicate delegate_task (already run {prior_count}×)."
                        if pc["tool_name"] == "delegate_task"
                        else f"Skipping duplicate read-only call to {pc['tool_name']} (already run {prior_count}×)."
                    ),
                )
                continue

            if dedupe_safe:
                batch_seen[fp] = idx
            to_run_indices.append(idx)
            if not is_read_only:
                mutation_before = True
                self._mutation_seen = True

        calls_to_run = [parsed_calls[i] for i in to_run_indices]
        run_results = await self.run_tool_batch(calls_to_run, hooks_data, hooks_manager)

        # Merge real results + dup short-circuit results back into original order
        results: list[Any] = [None] * len(parsed_calls)
        for i, r in zip(to_run_indices, run_results, strict=True):
            results[i] = r
        for i, placeholder in dup_results.items():
            src = placeholder.pop("_dup_of_batch_index", None)
            if src is not None and results[src] is not None:
                cloned = (
                    dict(results[src])
                    if isinstance(results[src], dict)
                    else {"output": results[src]}
                )
                cloned["_warning"] = placeholder.get("_warning", "Duplicate result reused.")
                results[i] = cloned
            else:
                placeholder["error"] = "Duplicate tool call skipped"
                results[i] = placeholder

        # Update call counters / last-result cache for future iterations via the
        # shared LoopGuard, and detect cross-iteration doom-loops here: if any
        # fingerprint has now been called past its hard threshold we signal the
        # loop to terminate after persisting the current results.
        doom_offender: Optional[tuple[str, int]] = None  # (tool_name, count)
        executed_indices = set(to_run_indices)
        for idx, (pc, res) in enumerate(zip(parsed_calls, results, strict=True)):
            fp_val = pc.get("_fp")
            if not fp_val or not isinstance(fp_val, str):
                continue
            fp = fp_val
            if not fp:
                continue
            if idx not in executed_indices:
                continue
            # User-denied calls don't reflect a stuck model — the user can
            # deny the same write 5× because they're reviewing each preview.
            # Treating denials as doom-loop hits produces a misleading
            # "stuck in a loop" stop instead of a clean "you keep denying".
            if isinstance(res, dict) and res.get("error_code") == ToolErrorCode.DENIED:
                continue
            count = self.loop_guard.record_execution(fp, res)
            if self.loop_guard.is_doom(pc["tool_name"], count) and (
                doom_offender is None or count > doom_offender[1]
            ):
                doom_offender = (pc["tool_name"], count)

        for pc in parsed_calls:
            cached_count = pc.get("_cached_repeat_count")
            if (
                isinstance(cached_count, int)
                and self.loop_guard.is_doom(pc["tool_name"], cached_count)
                and (doom_offender is None or cached_count > doom_offender[1])
            ):
                doom_offender = (pc["tool_name"], cached_count)

        for idx, (pc, res) in enumerate(zip(parsed_calls, results, strict=True)):
            if isinstance(res, dict) and res.get("success") is True:
                self._turn.warm_tool_names.add(pc["tool_name"])
            if self._turn.objective_state is not None:
                self._turn.objective_state.record_tool_result(
                    pc["tool_name"],
                    pc.get("arguments"),
                    res,
                    self.agent.tools.get(pc["tool_name"]),
                )
            useful_action = self._turn.record_first_useful_action(
                pc["tool_name"],
                res,
                executed=idx in executed_indices,
            )
            if useful_action is not None:
                get_services().events.emit("first_useful_action", **useful_action)
            # Pull any base64 image out BEFORE summarization so it reaches the
            # model as a real vision block instead of being truncated/stringified.
            res, images = _extract_vision_images(res)
            controller = self.runtime.context_controller
            if controller is not None:
                res = controller.summarize_tool_result(res)
            get_services().events.emit(
                "tool_result", tool_name=pc["tool_name"], result=res, tool_id=pc["tool_id"]
            )
            extra: dict[str, Any] = {"name": pc["tool_name"]}
            if images:
                extra["tool_images"] = images

            # Provenance (Phase 3.2): tool results that ingest outside data are
            # serialized inside a non-authoritative <untrusted_tool_output> block
            # and mark the turn as tainted so the egress gate (3.4) arms. The UI
            # event above still carries the clean dict — only the model-facing
            # transcript is fenced.
            serialized = json.dumps(res)
            if self._result_provenance(pc["tool_name"]) == Provenance.UNTRUSTED_EXTERNAL:
                # A static mcp_* tool (mcp_call_tool, mcp_read_resource, …) relays
                # third-party server output but has a local Tool object, so the
                # name-based proxy check misses it — self-declared mcp_source is
                # what arms the confused-deputy (MCP-mutation) gate for it.
                tool_obj = self.agent.tools.get(pc["tool_name"])
                from_mcp = is_mcp_function_name(pc["tool_name"]) or bool(
                    getattr(tool_obj, "mcp_source", False)
                )
                self._mark_turn_untrusted(from_mcp=from_mcp)
                serialized = wrap_untrusted_output(serialized, self._untrusted_source(pc))
            self.agent.session.add_message("tool", serialized, tool_call_id=pc["tool_id"], **extra)

        if self.agent.tracker_info:
            self.agent.tracker_update(current_tool=None)

        # Update the messages list from session
        messages.clear()
        messages.extend(self.agent.session.get_messages_for_api())

        # Detect which failures are user denials (not real errors).
        denied_tools: list[str] = []
        for pc, res in zip(parsed_calls, results, strict=True):
            if isinstance(res, dict) and res.get("error_code") == ToolErrorCode.DENIED:
                denied_tools.append(pc.get("tool_name", "unknown"))

        all_tool_calls_failed = bool(results) and all(
            not (isinstance(res, dict) and res.get("success") is True) for res in results
        )
        if all_tool_calls_failed:
            if denied_tools:
                get_services().events.emit(
                    "agent_warning",
                    message=f"Tool(s) denied by user: {', '.join(denied_tools)}. "
                    "Asking the model to try a different approach.",
                )
                return ToolBatchOutcome(BatchStatus.DENIED, denied_tools=denied_tools)
            get_services().events.emit(
                "agent_warning",
                message="All tool calls in this step failed. Asking the model to revise its plan.",
            )
            return ToolBatchOutcome(BatchStatus.RETRY)

        if denied_tools:
            return ToolBatchOutcome(BatchStatus.DENIED, denied_tools=denied_tools)

        if doom_offender is not None:
            tool_name, count = doom_offender
            get_services().events.emit("agent_warning", message=doom_message(tool_name, count))
            return ToolBatchOutcome(BatchStatus.DOOM_LOOP, doom_tool=tool_name, doom_count=count)

        return ToolBatchOutcome(BatchStatus.OK)
