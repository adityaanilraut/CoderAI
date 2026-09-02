"""Workflow Scripting Engine for CoderAI.

Provides high-scale subagent orchestration via sandboxed workflow scripts with
primitives for agent dispatch, streaming pipelines, parallel fan-out, phase
tracking, and structured JSON schema validation.

Semantics mirror the DeepSeek Harness workflow seam
(``packages/workflow/workflow-worker-thread``):

- Script hooks ``agent()`` / ``pipeline()`` / ``parallel()`` / ``phase()`` /
  ``log()`` + the ``args`` global.
- FIFO concurrency slots with the harness caps (``maxConcurrentAgents``
  auto-resolves to ``min(16, max(1, cores - 2))``, ``maxTotalAgents`` 1000,
  ``maxItemsPerCall`` 4096), overridable via ``CODERAI_WORKFLOW_*`` env vars.
- Fatal ``WorkflowError`` codes always kill the script; ordinary stage/thunk
  throws and child failures dissolve to per-item ``null``.
- Cancellation is a hook boundary: after ``cancel()`` every hook throws
  ``CANCELLED`` and queued ``agent()`` slot waiters reject.
- Stop reasons ``completed | cancelled | error``; ``execute_workflow_script``
  never raises for run outcomes (validation errors raise synchronously).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import textwrap
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Callable, Coroutine, Iterable, Sequence

from coderai.core.orchestration import (
    WorkflowLimits,
    get_orchestration_event_bus,
    resolve_workflow_limits,
)
from coderai.core.subagent import SubAgentManager, SubAgentResult, SubAgentSpec

logger = logging.getLogger(__name__)


class WorkflowErrorCode:
    """Machine-routable workflow failure codes."""

    SCRIPT_PARSE = "SCRIPT_PARSE"
    META_INVALID = "META_INVALID"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    UNSUPPORTED_OPTION = "UNSUPPORTED_OPTION"
    UNSUPPORTED_SCHEMA = "UNSUPPORTED_SCHEMA"
    AGENT_CAP = "AGENT_CAP"
    ITEM_CAP = "ITEM_CAP"
    AGENT_START = "AGENT_START"
    AGENT_RESULT = "AGENT_RESULT"
    RESULT_UNSERIALIZABLE = "RESULT_UNSERIALIZABLE"
    CANCELLED = "CANCELLED"


class WorkflowError(Exception):
    """Typed error for workflow execution failures."""

    def __init__(
        self,
        message: str,
        code: str = WorkflowErrorCode.AGENT_RESULT,
        fatal: bool = True,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.fatal = fatal


def is_fatal_workflow_error(error: Any) -> bool:
    """Return True if error is a fatal WorkflowError."""
    return isinstance(error, WorkflowError) and error.fatal


# ``agent()`` options the script may pass; everything else rejects loud.
SUPPORTED_AGENT_OPTIONS = frozenset(
    {
        "label",
        "phase",
        "schema",
        "provider",
        "model",
        # Legacy CoderAI options retained for backward compatibility.
        "description",
        "mode",
        "timeout_seconds",
        "max_iterations",
        "depth",
        "allowed_tools",
        "extra_context",
    }
)
# Deferred Claude Code-style options we name explicitly in the rejection.
DEFERRED_AGENT_OPTIONS = frozenset({"effort", "isolation", "agentType"})


@dataclass
class WorkflowPhase:
    """Represents a phase in a workflow execution."""

    title: str
    timestamp: float = field(default_factory=time.time)
    duration_seconds: float = 0.0


@dataclass
class WorkflowLog:
    """Represents a log entry in a workflow execution."""

    message: str
    timestamp: float = field(default_factory=time.time)
    level: str = "INFO"


@dataclass
class WorkflowResult:
    """Aggregated output from a workflow execution."""

    workflow_id: str
    name: str
    status: str  # "completed" | "failed" | "cancelled" (legacy: "interrupted"/"timeout")
    phases: list[WorkflowPhase] = field(default_factory=list)
    logs: list[WorkflowLog] = field(default_factory=list)
    output: Any = None
    error: str | None = None
    duration_seconds: float = 0.0
    agent_executions: int = 0
    total_tokens: int = 0

    @property
    def stop_reason(self) -> str:
        """Harness stop reason: ``completed | cancelled | error``."""
        return {"completed": "completed", "cancelled": "cancelled", "interrupted": "cancelled"}.get(
            self.status, "error"
        )

    @property
    def agents_started(self) -> int:
        return self.agent_executions

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "name": self.name,
            "status": self.status,
            "stopReason": self.stop_reason,
            "agentsStarted": self.agent_executions,
            "phases": [
                {"title": p.title, "timestamp": p.timestamp, "duration_seconds": p.duration_seconds}
                for p in self.phases
            ],
            "logs": [
                {
                    "message": log_entry.message,
                    "timestamp": log_entry.timestamp,
                    "level": log_entry.level,
                }
                for log_entry in self.logs
            ],
            "output": self.output,
            "error": self.error,
            "duration_seconds": self.duration_seconds,
            "agent_executions": self.agent_executions,
            "total_tokens": self.total_tokens,
        }

    def format_markdown(self) -> str:
        status_badge = "✅ COMPLETED" if self.status == "completed" else f"⚠️ {self.status.upper()}"
        lines = [
            f"### Workflow Execution: {self.name} [{self.workflow_id}] — {status_badge}",
            f"**Status**: `{self.status}` | **Duration**: `{self.duration_seconds:.2f}s` | **Agents Run**: `{self.agent_executions}` | **Tokens**: `{self.total_tokens}`",
        ]
        if self.phases:
            lines.append("\n**Phases**:")
            for p in self.phases:
                lines.append(f"- **{p.title}** ({p.duration_seconds:.2f}s)")
        if self.error:
            lines.append(f"\n> ❌ **Error**: {self.error}\n")
        if self.output is not None:
            lines.append("\n**Result Output**:")
            if isinstance(self.output, (dict, list)):
                rendered = json.dumps(self.output, indent=2)
                if len(rendered) > 50_000:
                    rendered = (
                        rendered[:50_000]
                        + f"\n… [truncated: {len(rendered) - 50_000} more characters]"
                    )
                lines.append(f"```json\n{rendered}\n```")
            else:
                lines.append(str(self.output).strip())
        if self.logs:
            lines.append("\n<details><summary>Workflow Logs</summary>\n")
            for log_entry in self.logs:
                lines.append(
                    f"- `[{time.strftime('%H:%M:%S', time.localtime(log_entry.timestamp))}]` {log_entry.message}"
                )
            lines.append("\n</details>")
        return "\n".join(lines)


class WorkflowContext:
    """Runtime context passed to and modified by workflow primitives."""

    def __init__(
        self,
        workflow_id: str,
        name: str,
        project_root: str,
        create_openai_client: Callable[[], dict[str, Any]] | None = None,
        parent_session_id: str | None = None,
        limits: WorkflowLimits | None = None,
    ) -> None:
        self.workflow_id = workflow_id
        self.name = name
        self.project_root = project_root
        self.create_openai_client = create_openai_client
        self.parent_session_id = parent_session_id
        self.limits = limits or resolve_workflow_limits()
        self.phases: list[WorkflowPhase] = []
        self.logs: list[WorkflowLog] = []
        self.agent_executions = 0
        self.total_tokens = 0
        self._current_phase_start = time.time()
        self.subagent_manager = (
            SubAgentManager(project_root=project_root, create_openai_client=create_openai_client)
            if create_openai_client
            else None
        )
        self._cancel_reason: str | None = None
        self._cancel_event: asyncio.Event | None = None

    # -- cancellation -------------------------------------------------------

    def cancel(self, reason: str = "parent step aborted") -> None:
        """Cancel the run: future hook calls throw CANCELLED; queued slot
        waiters reject. Idempotent; the first reason wins."""
        if self._cancel_reason is not None:
            return
        self._cancel_reason = reason
        if self._cancel_event is not None:
            self._cancel_event.set()

    def is_cancelled(self) -> bool:
        return self._cancel_reason is not None

    def cancelled_error(self) -> WorkflowError:
        return WorkflowError(
            f"workflow run cancelled: {self._cancel_reason or 'cancelled'}",
            WorkflowErrorCode.CANCELLED,
        )

    # -- progress -----------------------------------------------------------

    def phase(self, title: str) -> None:
        """Demarcate a phase transition in the workflow."""
        now = time.time()
        if self.phases:
            self.phases[-1].duration_seconds = max(0.0, now - self._current_phase_start)
        new_phase = WorkflowPhase(title=title, timestamp=now)
        self.phases.append(new_phase)
        self._current_phase_start = now
        self.log(f"Entering phase: {title}")

    def log(self, message: str, level: str = "INFO") -> None:
        """Record a workflow log entry."""
        self.logs.append(WorkflowLog(message=str(message), level=level))
        logger.info("[Workflow %s] %s", self.workflow_id, message)

    def finalize_phases(self) -> None:
        """Close duration of the last active phase."""
        now = time.time()
        if self.phases:
            self.phases[-1].duration_seconds = max(0.0, now - self._current_phase_start)


def _validate_schema(data: Any, schema: dict[str, Any]) -> tuple[bool, str | None]:
    """Lightweight JSON Schema validator for common types."""
    try:
        import jsonschema  # type: ignore

        jsonschema.validate(instance=data, schema=schema)
        return True, None
    except ImportError:
        # Fallback minimal validator
        expected_type = schema.get("type")
        if expected_type == "object" and not isinstance(data, dict):
            return False, f"Expected object, got {type(data).__name__}"
        if expected_type == "array" and not isinstance(data, list):
            return False, f"Expected array, got {type(data).__name__}"
        if expected_type == "string" and not isinstance(data, str):
            return False, f"Expected string, got {type(data).__name__}"
        if expected_type in ("number", "integer") and not isinstance(data, (int, float)):
            return False, f"Expected number, got {type(data).__name__}"
        if expected_type == "boolean" and not isinstance(data, bool):
            return False, f"Expected boolean, got {type(data).__name__}"
        if isinstance(data, dict) and "required" in schema:
            for req in schema["required"]:
                if req not in data:
                    return False, f"Missing required property '{req}'"
        return True, None
    except Exception as exc:
        return False, str(exc)


def _extract_json_from_text(text: str) -> Any:
    """Extract and parse JSON object or array from markdown code blocks or text."""
    text = text.strip()
    # Check for markdown fenced JSON
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except Exception:
            pass

    # Try raw parse
    try:
        return json.loads(text)
    except Exception:
        pass

    # Try searching for outermost { } or [ ]
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        try:
            return json.loads(text[first_brace : last_brace + 1])
        except Exception:
            pass

    first_bracket = text.find("[")
    last_bracket = text.rfind("]")
    if first_bracket != -1 and last_bracket != -1 and last_bracket > first_bracket:
        try:
            return json.loads(text[first_bracket : last_bracket + 1])
        except Exception:
            pass

    return None


def _materialize_result(value: Any) -> Any:
    """Reject non-JSON-serializable results with RESULT_UNSERIALIZABLE."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError) as exc:
        raise WorkflowError(
            "the workflow's return value is not plain JSON data — "
            f"{exc}. Return only JSON-serializable objects/arrays/scalars.",
            WorkflowErrorCode.RESULT_UNSERIALIZABLE,
        )


def _default_label(prompt: str) -> str:
    """A short display label derived from the prompt when the script passes none."""
    newline = prompt.find("\n")
    line = prompt if newline == -1 else prompt[:newline]
    return line if len(line) <= 48 else f"{line[:47]}…"


def _call_with_arity(stage: Callable[..., Any], value: Any, item: Any, index: int) -> Any:
    """Dispatch a pipeline stage by its declared parameter count.

    Harness contract is ``(prev, item, index)``; legacy CoderAI scripts used
    single-argument stages, which stay supported.
    """
    try:
        sig = inspect.signature(stage)
        count = len(
            [
                p
                for p in sig.parameters.values()
                if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
            ]
        )
    except (TypeError, ValueError):
        count = 3
    if count >= 3:
        return stage(value, item, index)
    if count == 2:
        return stage(value, item)
    return stage(value)


class WorkflowEngine:
    """Executes workflow orchestration scripts in an isolated, asynchronous environment."""

    def __init__(self, context: WorkflowContext) -> None:
        self.context = context
        self._active_slots = 0
        self._slot_waiters: list[asyncio.Future[Any]] = []
        self._current_phase: str | None = None
        self._event_bus = get_orchestration_event_bus()

    # -- cancellation / slot plumbing ---------------------------------------

    def _throw_if_cancelled(self) -> None:
        if self.context.is_cancelled():
            raise self.context.cancelled_error()

    def _acquire_slot(self) -> asyncio.Future[Any]:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        if self._active_slots < self.context.limits.max_concurrent_agents:
            self._active_slots += 1
            fut.set_result(None)
        else:
            self._slot_waiters.append(fut)
        return fut

    def _release_slot(self) -> None:
        self._active_slots -= 1
        while self._slot_waiters:
            waiter = self._slot_waiters.pop(0)
            if not waiter.done():
                self._active_slots += 1
                waiter.set_result(None)
                return

    def _reject_queued_waiters(self) -> None:
        for waiter in self._slot_waiters:
            if not waiter.done():
                waiter.set_exception(self.context.cancelled_error())
        self._slot_waiters.clear()

    # -- lifecycle publication ----------------------------------------------

    def _emit(self, event_name: str, payload: dict[str, Any]) -> None:
        self._event_bus.emit(event_name, payload)

    def _emit_agent_start(self, info: dict[str, Any]) -> None:
        self._emit("workflow/agent-start", {**info, "runId": self.context.workflow_id})

    def _emit_agent_end(self, info: dict[str, Any], outcome: str) -> None:
        self._emit(
            "workflow/agent-end",
            {**info, "runId": self.context.workflow_id, "outcome": outcome},
        )

    # -- agent() core -------------------------------------------------------

    def _read_agent_options(self, raw_opts: Any) -> dict[str, Any]:
        if raw_opts is None:
            return {}
        if not isinstance(raw_opts, dict):
            raise WorkflowError(
                "agent() options must be an object", WorkflowErrorCode.INVALID_ARGUMENT
            )
        opts: dict[str, Any] = {}
        for key, value in raw_opts.items():
            if key in SUPPORTED_AGENT_OPTIONS:
                opts[key] = value
                continue
            if key in DEFERRED_AGENT_OPTIONS:
                raise WorkflowError(
                    f'agent() option "{key}" is deferred and not supported by this engine '
                    f"(supported: {', '.join(sorted(SUPPORTED_AGENT_OPTIONS))})",
                    WorkflowErrorCode.UNSUPPORTED_OPTION,
                )
            raise WorkflowError(
                f'agent() option "{key}" is not recognized '
                f"(supported: {', '.join(sorted(SUPPORTED_AGENT_OPTIONS))})",
                WorkflowErrorCode.UNSUPPORTED_OPTION,
            )
        for key in ("label", "phase", "provider", "model", "description", "mode"):
            if key in opts and opts[key] is not None and not isinstance(opts[key], str):
                raise WorkflowError(
                    f'agent() option "{key}" must be a string', WorkflowErrorCode.INVALID_ARGUMENT
                )
        schema = opts.get("schema")
        if schema is not None and not isinstance(schema, dict):
            raise WorkflowError(
                "agent() schema must be an object-rooted JSON Schema",
                WorkflowErrorCode.UNSUPPORTED_SCHEMA,
            )
        return opts

    def _child_depth(self) -> int:
        """Lineage-derived child depth for workflow children."""
        from coderai.core.agents import get_agent_registry

        for handle in get_agent_registry().list():
            if getattr(handle, "run_session_id", None) == self.context.parent_session_id:
                return handle.depth + 1
        return 1

    async def _run_child(
        self, raw_prompt: str, opts: dict[str, Any], seq: int, label: str, phase: str | None
    ) -> tuple[SubAgentResult | None, dict[str, Any]]:
        """Shared child runner: slot acquisition, caps, lifecycle events."""
        info: dict[str, Any] = {"seq": seq, "label": label, "childId": None}
        if phase is not None:
            info["phase"] = phase

        prompt = raw_prompt
        schema = opts.get("schema")
        if isinstance(schema, dict):
            schema_str = json.dumps(schema, indent=2)
            prompt = (
                f"{prompt}\n\n"
                f"IMPORTANT: You MUST return your final conclusion as valid JSON adhering to this JSON Schema:\n"
                f"```json\n{schema_str}\n```\n"
                f"Provide ONLY the JSON response without markdown wrapping."
            )

        slot = self._acquire_slot()
        granted = False
        try:
            await slot  # queued waiters resume (or reject on cancellation) here
            granted = True
            self._throw_if_cancelled()
            manager = self.context.subagent_manager
            if manager is None:
                raise WorkflowError(
                    "agent() could not start a child: no subagent provider is available",
                    WorkflowErrorCode.AGENT_START,
                )
            spec = SubAgentSpec(
                description=opts.get("description")
                or (raw_prompt[:50] + "..." if len(raw_prompt) > 50 else raw_prompt),
                prompt=prompt,
                mode=opts.get("mode", "general"),
                timeout_seconds=float(opts.get("timeout_seconds", 90.0)),
                max_iterations=int(opts.get("max_iterations", 20)),
                depth=opts.get("depth")
                if isinstance(opts.get("depth"), int)
                else self._child_depth(),
                parent_session_id=self.context.parent_session_id,
                allowed_tools=opts.get("allowed_tools"),
                extra_context=opts.get("extra_context"),
            )
            try:
                result: SubAgentResult = await manager.spawn_subagent(spec)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.context.is_cancelled():
                    self._emit_agent_end(info, "cancelled")
                    raise self.context.cancelled_error()
                raise WorkflowError(
                    f"agent() could not start a child: {exc}", WorkflowErrorCode.AGENT_START
                )
            self.context.total_tokens += result.total_tokens
            info["childId"] = result.session_id
            self._emit_agent_start(info)
            return result, info
        finally:
            if granted:
                self._release_slot()

    async def agent_dsh(self, raw_prompt: Any, raw_opts: Any = None) -> Any:
        """Harness ``agent()`` hook: final text, validated object, or ``null``.

        Child failures resolve to ``None`` (scripts ``.filter(Boolean)``);
        misused options, tripped caps, and infrastructure failures raise fatal
        ``WorkflowError``s that kill the script.
        """
        self._throw_if_cancelled()
        if not isinstance(raw_prompt, str) or not raw_prompt.strip():
            raise WorkflowError(
                "agent() requires a non-empty prompt string", WorkflowErrorCode.INVALID_ARGUMENT
            )
        opts = self._read_agent_options(raw_opts)
        if self.context.agent_executions >= self.context.limits.max_total_agents:
            raise WorkflowError(
                f"this run reached its total agent cap ({self.context.limits.max_total_agents}) — "
                "a runaway-loop backstop; raise maxTotalAgents if the scale is intentional",
                WorkflowErrorCode.AGENT_CAP,
            )
        self.context.agent_executions += 1
        seq = self.context.agent_executions
        label = opts.get("label") or _default_label(raw_prompt)
        phase = opts.get("phase") or self._current_phase

        result, info = await self._run_child(raw_prompt, opts, seq, label, phase)
        if result is None:
            return None
        try:
            schema = opts.get("schema")
            if result.stop_reason == "completed":
                if isinstance(schema, dict):
                    structured = _extract_json_from_text(result.summary)
                    if structured is not None:
                        is_valid, _err = _validate_schema(structured, schema)
                        if is_valid:
                            self._emit_agent_end(info, "completed")
                            return structured
                    self._emit_agent_end(info, "failed")
                    return None
                self._emit_agent_end(info, "completed")
                return result.summary
            if self.context.is_cancelled():
                self._emit_agent_end(info, "cancelled")
                raise self.context.cancelled_error()
            self._emit_agent_end(info, "failed")
            return None
        except WorkflowError:
            raise
        except Exception as exc:
            if self.context.is_cancelled():
                self._emit_agent_end(info, "cancelled")
                raise self.context.cancelled_error()
            self._emit_agent_end(info, "failed")
            raise WorkflowError(f"child agent run failed: {exc}", WorkflowErrorCode.AGENT_RESULT)

    async def agent(self, prompt: str, opts: dict[str, Any] | None = None) -> dict[str, Any]:
        """Legacy dict-returning agent primitive (backward-compatible view over
        the harness ``agent()`` core)."""
        opts = dict(opts or {})
        schema = opts.get("schema")

        if self.context.subagent_manager is None:
            # Mock or offline execution (legacy behavior)
            self.context.agent_executions += 1
            description = opts.get("description") or (
                prompt[:50] + "..." if len(prompt) > 50 else prompt
            )
            self.context.log(f"Executing agent (offline/mock): {description}")
            return {
                "task_id": "mock_task",
                "session_id": "mock_session",
                "status": "completed",
                "summary": f"Mock output for prompt: {prompt[:80]}",
                "data": None,
                "artifacts": [],
                "total_tokens": 0,
            }

        parsed_opts = self._read_agent_options(opts)
        if self.context.agent_executions >= self.context.limits.max_total_agents:
            raise WorkflowError(
                f"this run reached its total agent cap ({self.context.limits.max_total_agents}) — "
                "a runaway-loop backstop; raise maxTotalAgents if the scale is intentional",
                WorkflowErrorCode.AGENT_CAP,
            )
        self.context.agent_executions += 1
        seq = self.context.agent_executions
        label = parsed_opts.get("label") or _default_label(prompt)
        result, _info = await self._run_child(
            prompt, parsed_opts, seq, label, parsed_opts.get("phase")
        )
        if result is None:
            return {
                "task_id": "mock_task",
                "session_id": "mock_session",
                "status": "failed",
                "summary": "agent() could not start a child.",
                "data": None,
                "artifacts": [],
                "total_tokens": 0,
            }

        parsed_data = None
        if isinstance(schema, dict):
            parsed = _extract_json_from_text(result.summary)
            if parsed is not None:
                is_valid, _err = _validate_schema(parsed, schema)
                if is_valid:
                    parsed_data = parsed
        return {
            "task_id": result.task_id,
            "session_id": result.session_id,
            "status": result.status,
            "summary": result.summary,
            "data": parsed_data,
            "error": result.error,
            "artifacts": result.artifacts,
            "iterations": result.iterations,
            "total_tokens": result.total_tokens,
        }

    # -- combinators --------------------------------------------------------

    def _assert_item_cap(self, length: int, hook: str) -> None:
        if length > self.context.limits.max_items_per_call:
            raise WorkflowError(
                f"{hook} received {length} items — over the per-call cap "
                f"({self.context.limits.max_items_per_call}); split the work or raise "
                "maxItemsPerCall in the engine config",
                WorkflowErrorCode.ITEM_CAP,
            )

    async def pipeline(self, items: Iterable[Any], *stages: Callable[..., Any]) -> list[Any]:
        """Streaming async pipeline running items through stages without inter-stage barriers.

        Harness semantics: each item runs its stage chain independently
        (stage signature ``(prev, item, index)``; legacy single-argument stages
        stay supported). An ordinary stage throw drops the ITEM to ``null`` and
        skips its remaining stages; a fatal ``WorkflowError`` kills the script.
        """
        self._throw_if_cancelled()
        item_list = list(items)
        if not stages:
            raise WorkflowError(
                "pipeline() requires at least one stage function",
                WorkflowErrorCode.INVALID_ARGUMENT,
            )
        self._assert_item_cap(len(item_list), "pipeline()")
        if not item_list:
            return []
        for stage_idx, stage in enumerate(stages):
            if not callable(stage):
                raise WorkflowError(
                    f"pipeline() stage {stage_idx} is not a function",
                    WorkflowErrorCode.INVALID_ARGUMENT,
                )

        async def _run_item_through_stages(index: int, item: Any) -> Any:
            current = item
            for stage in stages:
                self._throw_if_cancelled()
                try:
                    res = _call_with_arity(stage, current, item, index)
                    if inspect.isawaitable(res):
                        current = await res
                    else:
                        current = res
                except WorkflowError:
                    raise
                except (asyncio.CancelledError,):
                    raise
                except Exception:
                    # Ordinary stage throw drops the item to null and skips
                    # its remaining stages.
                    return None
            return current

        tasks = [_run_item_through_stages(i, it) for i, it in enumerate(item_list)]
        return list(await asyncio.gather(*tasks))

    async def parallel(
        self,
        thunks: Sequence[Callable[[], Any] | Coroutine[Any, Any, Any]],
        max_concurrency: int | None = None,
    ) -> list[Any]:
        """Concurrently await thunk executions with bounded concurrency.

        Harness semantics: a throwing thunk resolves to ``null``; fatal
        ``WorkflowError``s propagate and kill the script.
        """
        self._throw_if_cancelled()
        if not thunks:
            return []
        self._assert_item_cap(len(thunks), "parallel()")

        semaphore = (
            asyncio.Semaphore(max_concurrency) if max_concurrency and max_concurrency > 0 else None
        )

        async def _run_thunk(thunk: Any) -> Any:
            try:
                if semaphore:
                    async with semaphore:
                        self._throw_if_cancelled()
                        return await _eval_thunk(thunk)
                self._throw_if_cancelled()
                return await _eval_thunk(thunk)
            except WorkflowError:
                raise
            except (asyncio.CancelledError,):
                raise
            except Exception:
                # Ordinary thunk throw → null (the script filters with
                # .filter(Boolean)); fatal WorkflowErrors re-raise above.
                return None

        async def _eval_thunk(thunk: Any) -> Any:
            if inspect.iscoroutine(thunk):
                return await thunk
            elif inspect.iscoroutinefunction(thunk):
                return await thunk()
            elif callable(thunk):
                res = thunk()
                if inspect.iscoroutine(res):
                    return await res
                return res
            return thunk

        tasks = [_run_thunk(t) for t in thunks]
        return list(await asyncio.gather(*tasks))


# ---------------------------------------------------------------------------
# Script execution
# ---------------------------------------------------------------------------


def _workflow_globals(
    engine: WorkflowEngine, context: WorkflowContext, args_dict: dict[str, Any]
) -> dict[str, Any]:
    return {
        "__name__": "__workflow__",
        "__doc__": None,
        "__builtins__": {
            # Safe builtins
            "abs": abs,
            "all": all,
            "any": any,
            "bool": bool,
            "dict": dict,
            "enumerate": enumerate,
            "filter": filter,
            "float": float,
            "int": int,
            "isinstance": isinstance,
            "issubclass": issubclass,
            "iter": iter,
            "len": len,
            "list": list,
            "map": map,
            "max": max,
            "min": min,
            "next": next,
            "print": lambda *a, **k: context.log(" ".join(str(x) for x in a)),
            "range": range,
            "repr": repr,
            "reversed": reversed,
            "round": round,
            "set": set,
            "slice": slice,
            "sorted": sorted,
            "str": str,
            "sum": sum,
            "tuple": tuple,
            "type": type,
            "zip": zip,
            "Exception": Exception,
            "ValueError": ValueError,
            "TypeError": TypeError,
            "KeyError": KeyError,
            "IndexError": IndexError,
            "RuntimeError": RuntimeError,
        },
        "json": json,
        "asyncio": asyncio,
        "re": re,
        "time": time,
        "uuid": uuid,
        # Workflow primitives (harness hooks + legacy context access)
        "agent": engine.agent_dsh,
        "pipeline": engine.pipeline,
        "parallel": engine.parallel,
        "phase": context.phase,
        "log": context.log,
        "args": args_dict,
        "context": context,
    }


async def _execute_legacy_script(
    script: str, env_globals: dict[str, Any], context: WorkflowContext
) -> Any:
    """Legacy path: ``exec`` + optional ``main``/``output``/``result``."""
    compiled = compile(script, "<workflow_script>", "exec")
    local_scope: dict[str, Any] = {}
    exec(compiled, env_globals, local_scope)  # noqa: S102 - curated namespace

    output: Any = None
    if "main" in local_scope and callable(local_scope["main"]):
        main_fn = local_scope["main"]
        sig = inspect.signature(main_fn)
        if len(sig.parameters) > 0:
            res = main_fn(env_globals["args"])
        else:
            res = main_fn()
        if inspect.iscoroutine(res):
            output = await res
        else:
            output = res
    elif "output" in local_scope:
        output = local_scope["output"]
    elif "result" in local_scope:
        output = local_scope["result"]
    return output


async def _execute_dsh_script(
    script: str, env_globals: dict[str, Any], context: WorkflowContext
) -> Any:
    """Harness path: the body is an async function body (top-level ``await``
    and ``return <value>`` allowed)."""
    indented = textwrap.indent(script, "    ")
    wrapped = f"async def __workflow__():\n{indented}\n"
    compiled = compile(wrapped, "<workflow_script>", "exec")
    local_scope: dict[str, Any] = {}
    exec(compiled, env_globals, local_scope)  # noqa: S102 - curated namespace
    fn = local_scope["__workflow__"]
    return await fn()


async def execute_workflow_script(
    script: str,
    args: dict[str, Any] | None,
    context: WorkflowContext,
) -> WorkflowResult:
    """Compile and execute a workflow script with primitives injected into scope.

    Supports the legacy CoderAI shape (``async def main(args)`` or top-level
    ``output``/``result``) and the DeepSeek Harness shape (top-level await with
    ``return <value>``). Run outcomes never raise: they resolve as
    ``completed``/``cancelled``/``failed`` results. Cancellation is observed at
    hook boundaries and at settlement.
    """
    start_time = time.time()
    engine = WorkflowEngine(context)
    args_dict = args or {}
    env_globals = _workflow_globals(engine, context, args_dict)

    run_id = context.workflow_id
    engine._emit(
        "workflow/start",
        {"runId": run_id, "name": context.name, "meta": {"name": context.name}},
    )

    def _finish(status: str, output: Any, error: str | None) -> WorkflowResult:
        context.finalize_phases()
        duration = max(0.0, time.time() - start_time)
        result = WorkflowResult(
            workflow_id=context.workflow_id,
            name=context.name,
            status=status,
            phases=context.phases,
            logs=context.logs,
            output=output,
            error=error,
            duration_seconds=duration,
            agent_executions=context.agent_executions,
            total_tokens=context.total_tokens,
        )
        engine._emit(
            "workflow/end",
            {
                "runId": run_id,
                "stopReason": result.stop_reason,
                "agentsStarted": context.agent_executions,
                **({"error": error} if error else {}),
            },
        )
        return result

    if context.is_cancelled():
        return _finish("cancelled", None, str(context.cancelled_error()))

    try:
        try:
            try:
                output = await _execute_legacy_script(script, env_globals, context)
            except SyntaxError as legacy_err:
                # Retry under the harness wrapper: top-level await/return.
                try:
                    output = await _execute_dsh_script(script, env_globals, context)
                except SyntaxError:
                    raise WorkflowError(
                        f"workflow script does not parse: {legacy_err}",
                        WorkflowErrorCode.SCRIPT_PARSE,
                    )
        except WorkflowError:
            if context.is_cancelled():
                return _finish("cancelled", None, str(context.cancelled_error()))
            raise
        if context.is_cancelled():
            # A script that settled without touching another hook must still
            # report cancelled — completed would be a lie.
            return _finish("cancelled", None, str(context.cancelled_error()))
        output = None if output is None else _materialize_result(output)
        return _finish("completed", output, None)
    except asyncio.CancelledError:
        context.cancel("workflow execution was cancelled")
        engine._reject_queued_waiters()
        return _finish("cancelled", None, "Workflow execution was cancelled.")
    except Exception as exc:
        logger.exception("Workflow script execution failed")
        engine._reject_queued_waiters()
        if context.is_cancelled():
            return _finish("cancelled", None, str(context.cancelled_error()))
        if isinstance(exc, WorkflowError):
            return _finish("failed", None, f"{type(exc).__name__}[{exc.code}]: {exc}")
        return _finish("failed", None, f"{type(exc).__name__}: {exc}")
