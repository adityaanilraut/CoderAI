"""Workflow Scripting Engine for CoderAI.

Provides high-scale subagent orchestration via sandboxed workflow scripts with
primitives for agent dispatch, streaming pipelines, parallel fan-out, phase tracking,
and structured JSON schema validation.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Callable, Coroutine, Iterable, Sequence

from coderai.core.subagent import SubAgentManager, SubAgentResult, SubAgentSpec

logger = logging.getLogger(__name__)


class WorkflowErrorCode:
    """Machine-routable workflow failure codes (DeepSeek Harness parity)."""

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
    status: str  # "completed" | "failed" | "interrupted" | "timeout"
    phases: list[WorkflowPhase] = field(default_factory=list)
    logs: list[WorkflowLog] = field(default_factory=list)
    output: Any = None
    error: str | None = None
    duration_seconds: float = 0.0
    agent_executions: int = 0
    total_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "name": self.name,
            "status": self.status,
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
                lines.append(f"```json\n{json.dumps(self.output, indent=2)}\n```")
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
    ) -> None:
        self.workflow_id = workflow_id
        self.name = name
        self.project_root = project_root
        self.create_openai_client = create_openai_client
        self.parent_session_id = parent_session_id
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


class WorkflowEngine:
    """Executes workflow orchestration scripts in an isolated, asynchronous environment."""

    def __init__(self, context: WorkflowContext) -> None:
        self.context = context

    async def agent(self, prompt: str, opts: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run an isolated subagent and optionally enforce a JSON schema on output."""
        opts = opts or {}
        description = opts.get("description") or (
            prompt[:50] + "..." if len(prompt) > 50 else prompt
        )
        mode = opts.get("mode", "general")
        timeout_seconds = float(opts.get("timeout_seconds", 90.0))
        max_iterations = int(opts.get("max_iterations", 20))
        depth = int(opts.get("depth", 1))
        allowed_tools = opts.get("allowed_tools")
        extra_context = opts.get("extra_context")
        schema = opts.get("schema")

        if schema:
            schema_str = json.dumps(schema, indent=2)
            prompt = (
                f"{prompt}\n\n"
                f"IMPORTANT: You MUST return your final conclusion as valid JSON adhering to this JSON Schema:\n"
                f"```json\n{schema_str}\n```\n"
                f"Provide ONLY the JSON response without markdown wrapping."
            )

        if not self.context.subagent_manager:
            # Mock or offline execution
            self.context.agent_executions += 1
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

        spec = SubAgentSpec(
            description=description,
            prompt=prompt,
            mode=mode,
            timeout_seconds=timeout_seconds,
            max_iterations=max_iterations,
            depth=depth,
            parent_session_id=self.context.parent_session_id,
            allowed_tools=allowed_tools,
            extra_context=extra_context,
        )

        self.context.agent_executions += 1
        self.context.log(f"Spawned subagent [{spec.task_id}]: {description}")
        result: SubAgentResult = await self.context.subagent_manager.spawn_subagent(spec)
        self.context.total_tokens += result.total_tokens

        parsed_data = None
        if schema:
            parsed = _extract_json_from_text(result.summary)
            if parsed is not None:
                is_valid, validation_err = _validate_schema(parsed, schema)
                if is_valid:
                    parsed_data = parsed
                else:
                    self.context.log(
                        f"Subagent [{spec.task_id}] schema validation error: {validation_err}",
                        level="WARNING",
                    )
            else:
                self.context.log(
                    f"Subagent [{spec.task_id}] failed to return valid JSON for schema.",
                    level="WARNING",
                )

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

    async def pipeline(self, items: Iterable[Any], *stages: Callable[..., Any]) -> list[Any]:
        """Streaming async pipeline running items through stages without inter-stage barriers.

        Each item is processed through stage_0 -> stage_1 -> ... -> stage_k.
        Items enter subsequent stages as soon as ready, enabling true streaming fan-out.
        """
        item_list = list(items)
        if not item_list or not stages:
            return item_list

        async def _run_item_through_stages(index: int, item: Any) -> tuple[int, Any]:
            current = item
            for stage_idx, stage in enumerate(stages):
                if inspect.iscoroutinefunction(stage):
                    current = await stage(current)
                elif callable(stage):
                    res = stage(current)
                    if inspect.iscoroutine(res):
                        current = await res
                    else:
                        current = res
                else:
                    raise TypeError(
                        f"Pipeline stage {stage_idx} must be a callable or coroutine function"
                    )
            return index, current

        tasks = [_run_item_through_stages(i, it) for i, it in enumerate(item_list)]
        results_with_idx = await asyncio.gather(*tasks)
        results_with_idx.sort(key=lambda x: x[0])
        return [res for _, res in results_with_idx]

    async def parallel(
        self,
        thunks: Sequence[Callable[[], Any] | Coroutine[Any, Any, Any]],
        max_concurrency: int | None = None,
    ) -> list[Any]:
        """Concurrently await thunk executions (coroutines or callables) with bounded concurrency."""
        if not thunks:
            return []

        semaphore = (
            asyncio.Semaphore(max_concurrency) if max_concurrency and max_concurrency > 0 else None
        )

        async def _run_thunk(thunk: Any) -> Any:
            if semaphore:
                async with semaphore:
                    return await _eval_thunk(thunk)
            return await _eval_thunk(thunk)

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


async def execute_workflow_script(
    script: str,
    args: dict[str, Any] | None,
    context: WorkflowContext,
) -> WorkflowResult:
    """Compile and execute a workflow script with primitives injected into scope."""
    start_time = time.time()
    engine = WorkflowEngine(context)
    args_dict = args or {}

    # Sandbox environment globals
    env_globals: dict[str, Any] = {
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
        # Workflow Primitives
        "agent": engine.agent,
        "pipeline": engine.pipeline,
        "parallel": engine.parallel,
        "phase": context.phase,
        "log": context.log,
        "args": args_dict,
        "context": context,
    }

    try:
        # Check if the script contains a main function or top-level statements
        compiled = compile(script, "<workflow_script>", "exec")
        local_scope: dict[str, Any] = {}
        exec(compiled, env_globals, local_scope)

        output: Any = None
        if "main" in local_scope and callable(local_scope["main"]):
            main_fn = local_scope["main"]
            # Call main with or without args depending on signature
            sig = inspect.signature(main_fn)
            if len(sig.parameters) > 0:
                res = main_fn(args_dict)
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

        context.finalize_phases()
        duration = max(0.0, time.time() - start_time)
        return WorkflowResult(
            workflow_id=context.workflow_id,
            name=context.name,
            status="completed",
            phases=context.phases,
            logs=context.logs,
            output=output,
            duration_seconds=duration,
            agent_executions=context.agent_executions,
            total_tokens=context.total_tokens,
        )
    except asyncio.CancelledError:
        context.finalize_phases()
        duration = max(0.0, time.time() - start_time)
        return WorkflowResult(
            workflow_id=context.workflow_id,
            name=context.name,
            status="interrupted",
            phases=context.phases,
            logs=context.logs,
            error="Workflow execution was cancelled.",
            duration_seconds=duration,
            agent_executions=context.agent_executions,
            total_tokens=context.total_tokens,
        )
    except Exception as exc:
        logger.exception("Workflow script execution failed")
        context.finalize_phases()
        duration = max(0.0, time.time() - start_time)
        return WorkflowResult(
            workflow_id=context.workflow_id,
            name=context.name,
            status="failed",
            phases=context.phases,
            logs=context.logs,
            error=f"{type(exc).__name__}: {exc}",
            duration_seconds=duration,
            agent_executions=context.agent_executions,
            total_tokens=context.total_tokens,
        )
