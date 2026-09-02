"""Sub-Agent Architecture & Engine for CoderAI.

Provides reliable sub-agent spawning, context isolation, tool/permission sandboxing,
parallel execution, cancellation/timeout recovery, and result aggregation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import uuid
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Callable

from coderai.core.common.message_converter import OpenAIMessageConverter
from coderai.core.common.openai_thinking import build_thinking_request_options
from coderai.core.common.usage import extract_usage_dict
from coderai.core.orchestration import (
    publish_subagent_end,
    publish_subagent_start,
    status_to_stop_reason,
)
from coderai.core.prompt import get_runtime_context, get_subagent_system_prompt, get_tools
from coderai.core.spawn import (
    DEFAULT_MAX_SUBAGENT_DEPTH,
    DEFAULT_SUBAGENT_MAX_ITERATIONS,
    DEFAULT_SUBAGENT_TIMEOUT_SECONDS,
    SubagentDescriptor,
    check_subagent_depth_quota,
    setup_subagent_scratchpad,
)
from coderai.core.state import clear_session_state
from coderai.core.tools.types import ToolExecutionHooks

logger = logging.getLogger(__name__)

MAX_SUBAGENT_ITERATIONS = DEFAULT_SUBAGENT_MAX_ITERATIONS
MAX_SUBAGENT_DEPTH = DEFAULT_MAX_SUBAGENT_DEPTH
DEFAULT_SUBAGENT_TIMEOUT = DEFAULT_SUBAGENT_TIMEOUT_SECONDS


@dataclass
class SubAgentSpec:
    """Specification for spawning an isolated sub-agent."""

    description: str
    prompt: str
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    mode: str = "read_only"  # "read_only" | "general"
    provider: str = "in_process"  # "in_process" | "acp" | "claude_code" | "codex"
    timeout_seconds: float = DEFAULT_SUBAGENT_TIMEOUT
    max_iterations: int = MAX_SUBAGENT_ITERATIONS
    depth: int = 0
    max_depth: int = MAX_SUBAGENT_DEPTH
    token_budget: int | None = None
    max_tokens: int | None = None
    isolated_cwd: str | None = None
    scratchpad_dir: str | None = None
    dry_run: bool = False
    parent_session_id: str | None = None
    allowed_tools: list[str] | None = None
    extra_context: str | None = None
    agent_id: str | None = None
    parent_agent_id: str | None = None
    root_agent_id: str | None = None
    children_ids: list[str] = field(default_factory=list)
    handle: Any | None = None
    seed_messages: list[dict[str, Any]] | None = None
    seed_events: list[Any] | None = None
    descriptor: SubagentDescriptor | None = None
    continuable: bool = False  # parked worker loop; survives multiple turns via its handle inbox

    def __post_init__(self) -> None:
        pass


@dataclass
class SubAgentResult:
    """Aggregated output from a sub-agent execution."""

    task_id: str
    session_id: str
    status: str  # "completed" | "failed" | "interrupted" | "timeout" | "max_iterations" | "budget_exceeded"
    summary: str
    active_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    iterations: int = 0
    tool_calls_count: int = 0
    duration_seconds: float = 0.0
    error: str | None = None
    artifacts: list[str] = field(default_factory=list)
    diffs: list[dict[str, Any]] = field(default_factory=list)
    exit_code: int = 0
    token_telemetry: dict[str, Any] = field(default_factory=dict)
    parent_agent_id: str | None = None
    root_agent_id: str | None = None
    depth: int = 0
    children_ids: list[str] = field(default_factory=list)
    lifecycle_events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def stop_reason(self) -> str:
        """Harness stop-reason vocabulary derived from the internal status.

        Mirrors ``SubagentStopReason``: ``completed | aborted | error |
        max-tokens | refusal``.
        """
        return status_to_stop_reason(self.status)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "status": self.status,
            "stopReason": self.stop_reason,
            "summary": self.summary,
            "active_tokens": self.active_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
            "iterations": self.iterations,
            "tool_calls_count": self.tool_calls_count,
            "duration_seconds": self.duration_seconds,
            "error": self.error,
            "artifacts": self.artifacts,
            "diffs": self.diffs,
            "exit_code": self.exit_code,
            "token_telemetry": self.token_telemetry,
            "parent_agent_id": self.parent_agent_id,
            "root_agent_id": self.root_agent_id,
            "depth": self.depth,
            "children_ids": self.children_ids,
            "lifecycle_events": self.lifecycle_events,
        }

    def format_markdown(self) -> str:
        status_badge = "✅ COMPLETED" if self.status == "completed" else f"⚠️ {self.status.upper()}"
        lines = [
            f"### Sub-Agent Task Result [{self.task_id}] — {status_badge}",
            f"**Status**: `{self.status}` | **Exit Code**: `{self.exit_code}` | **Iterations**: `{self.iterations}` | **Tokens**: `{self.total_tokens}`",
        ]
        if self.error:
            lines.append(f"\n> ❌ **Error**: {self.error}\n")
        lines.append("\n**Findings & Summary**:")
        lines.append(self.summary.strip() or "No summary provided.")
        if self.artifacts:
            lines.append("\n**Artifacts/Files Examined**:")
            for art in self.artifacts:
                lines.append(f"- `{art}`")
        if self.diffs:
            lines.append(f"\n**Files Modified ({len(self.diffs)})**:")
            for d in self.diffs:
                fp = d.get("file_path", "unknown")
                lines.append(f"- `{fp}`")
        return "\n".join(lines)


_global_active_controllers: dict[str, asyncio.Event] = {}


class SubAgentManager:
    """Manages sub-agent lifecycle, sandboxed execution, and parallel concurrency."""

    def __init__(
        self,
        project_root: str,
        create_openai_client: Callable[[], dict[str, Any]],
        get_resolved_settings: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.project_root = str(pathlib.Path(project_root).resolve())
        self.create_openai_client = create_openai_client
        self.get_resolved_settings = get_resolved_settings or (lambda: {})
        self.message_converter = OpenAIMessageConverter()
        self._active_controllers: dict[str, asyncio.Event] = {}

    def _emit_lifecycle_event(
        self,
        events: list[dict[str, Any]],
        event_type: str,
        spec: SubAgentSpec,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        evt: dict[str, Any] = {
            "type": event_type,
            "agent_id": spec.agent_id or spec.task_id,
            "task_id": spec.task_id,
            "depth": spec.depth,
            "parent_agent_id": spec.parent_agent_id,
            "root_agent_id": spec.root_agent_id,
            "timestamp": asyncio.get_event_loop().time()
            if asyncio.get_event_loop().is_running()
            else 0.0,
            "data": data or {},
        }
        events.append(evt)
        if spec.handle and hasattr(spec.handle, "lifecycle_history"):
            spec.handle.lifecycle_history.append(evt)
        return evt

    def cancel_subagent(self, session_id: str) -> None:
        """Cancel a running sub-agent session across all active manager scopes."""
        event = self._active_controllers.get(session_id) or _global_active_controllers.get(
            session_id
        )
        if event is not None:
            event.set()

    def cancel_all(self) -> None:
        """Cancel all running sub-agents globally."""
        for event in list(self._active_controllers.values()):
            event.set()
        for event in list(_global_active_controllers.values()):
            event.set()

    async def spawn_subagent(self, spec: SubAgentSpec) -> SubAgentResult:
        """Spawn and execute a single isolated sub-agent with timeout, quota checks, and error recovery.

        Publishes the harness ``subagent/start`` + ``subagent/end`` lifecycle
        pair around the run. A start-time quota rejection fails before
        publication (mirroring the reference's pre-publication rejection), so
        no lifecycle edge pair is emitted for a child that never existed.
        """
        session_id = (
            f"sub_{spec.parent_session_id[:8] if spec.parent_session_id else 'root'}_{spec.task_id}"
        )
        effective_max_depth = spec.max_depth if spec.max_depth is not None else MAX_SUBAGENT_DEPTH
        quota_ok, _quota_err = check_subagent_depth_quota(spec.depth, effective_max_depth)
        provider = spec.provider or "in_process"
        local = provider == "in_process"
        run_id = uuid.uuid4().hex
        if quota_ok:
            publish_subagent_start(
                run_id=run_id,
                provider=provider,
                child_id=session_id,
                local=local,
                parent_session_id=spec.parent_session_id,
            )
        result = await self._spawn_subagent_inner(spec, session_id)
        if quota_ok:
            partial_ok = result.status in (
                "completed",
                "refusal",
                "max_iterations",
                "budget_exceeded",
            )
            last_message = (
                [{"type": "text", "text": result.summary}]
                if result.summary and partial_ok
                else None
            )
            publish_subagent_end(
                run_id=run_id,
                provider=provider,
                child_id=session_id,
                local=local,
                stop_reason=result.stop_reason,
                last_assistant_message=last_message,
                parent_session_id=spec.parent_session_id,
            )
        return result

    async def _spawn_subagent_inner(self, spec: SubAgentSpec, session_id: str) -> SubAgentResult:
        """Unpublished spawn body: quota, scratchpad, controller, backend run."""
        lifecycle_events: list[dict[str, Any]] = []
        self._emit_lifecycle_event(
            lifecycle_events,
            "subagent/spawn",
            spec,
            {"description": spec.description, "mode": spec.mode, "depth": spec.depth},
        )

        effective_max_depth = spec.max_depth if spec.max_depth is not None else MAX_SUBAGENT_DEPTH
        quota_ok, quota_err = check_subagent_depth_quota(spec.depth, effective_max_depth)
        if not quota_ok:
            self._emit_lifecycle_event(
                lifecycle_events,
                "subagent/error",
                spec,
                {
                    "error": quota_err
                    or f"RecursionLimitError: Maximum sub-agent nesting depth exceeded (max {effective_max_depth})."
                },
            )
            return SubAgentResult(
                task_id=spec.task_id,
                session_id=session_id,
                status="failed",
                summary="Maximum sub-agent nesting depth exceeded.",
                error=quota_err
                or f"RecursionLimitError: Sub-agent depth {spec.depth} exceeds max_depth {effective_max_depth}.",
                exit_code=1,
                parent_agent_id=spec.parent_agent_id,
                root_agent_id=spec.root_agent_id,
                depth=spec.depth,
                children_ids=list(spec.children_ids),
                lifecycle_events=lifecycle_events,
            )

        if not spec.scratchpad_dir:
            spec.scratchpad_dir = setup_subagent_scratchpad(self.project_root, session_id)

        abort_event = asyncio.Event()
        self._active_controllers[session_id] = abort_event
        _global_active_controllers[session_id] = abort_event

        self._emit_lifecycle_event(
            lifecycle_events,
            "subagent/start",
            spec,
            {"timeout_seconds": spec.timeout_seconds, "max_iterations": spec.max_iterations},
        )

        from coderai.core.hooks import run_on_subagent_spawn

        try:
            run_on_subagent_spawn(
                parent_session_id=spec.parent_agent_id or "root",
                subagent_id=session_id,
                task=spec.prompt or spec.description,
                mode=spec.mode,
                project_root=self.project_root,
            )
        except Exception:
            pass

        try:
            if spec.provider == "claude_code":
                from coderai.core.subagent_backends.claude_code import (
                    ClaudeCodeDriver,
                    ClaudeCodeConfig,
                )

                claude_driver = ClaudeCodeDriver(
                    ClaudeCodeConfig(
                        timeout_seconds=spec.timeout_seconds,
                        cwd=self.project_root,
                    )
                )
                raw_res = await asyncio.wait_for(
                    claude_driver.execute(spec.prompt, project_root=self.project_root),
                    timeout=spec.timeout_seconds,
                )
                status = raw_res.get("status", "completed" if raw_res.get("ok") else "failed")
                result = SubAgentResult(
                    task_id=spec.task_id,
                    session_id=session_id,
                    status=status,
                    summary=raw_res.get("summary", ""),
                    error=raw_res.get("error"),
                    exit_code=0 if status == "completed" else 1,
                    duration_seconds=raw_res.get("duration_seconds", 0.0),
                )
            elif spec.provider == "codex":
                from coderai.core.subagent_backends.codex import CodexDriver, CodexConfig

                codex_driver = CodexDriver(
                    CodexConfig(
                        timeout_seconds=spec.timeout_seconds,
                        cwd=self.project_root,
                    )
                )
                raw_res = await asyncio.wait_for(
                    codex_driver.execute(spec.prompt, project_root=self.project_root),
                    timeout=spec.timeout_seconds,
                )
                status = raw_res.get("status", "completed" if raw_res.get("ok") else "failed")
                result = SubAgentResult(
                    task_id=spec.task_id,
                    session_id=session_id,
                    status=status,
                    summary=raw_res.get("summary", ""),
                    error=raw_res.get("error"),
                    exit_code=0 if status == "completed" else 1,
                    duration_seconds=raw_res.get("duration_seconds", 0.0),
                )
            elif spec.provider == "acp":
                from coderai.core.acp.runner import AcpSubagentRunner, AcpRunConfig

                runner = AcpSubagentRunner(
                    AcpRunConfig(
                        command="acp-agent",
                        cwd=self.project_root,
                        timeout_seconds=spec.timeout_seconds,
                    )
                )
                raw_res = await asyncio.wait_for(
                    runner.execute(spec.prompt),
                    timeout=spec.timeout_seconds,
                )
                status = raw_res.get("status", "completed" if raw_res.get("ok") else "failed")
                result = SubAgentResult(
                    task_id=spec.task_id,
                    session_id=session_id,
                    status=status,
                    summary=raw_res.get("summary", ""),
                    error=raw_res.get("error"),
                    exit_code=0 if status == "completed" else 1,
                    duration_seconds=raw_res.get("duration_seconds", 0.0),
                )
            else:
                result = await asyncio.wait_for(
                    self._run_subagent_loop(spec, session_id, abort_event, lifecycle_events),
                    timeout=spec.timeout_seconds,
                )
            result.parent_agent_id = spec.parent_agent_id
            result.root_agent_id = spec.root_agent_id
            result.depth = spec.depth
            result.children_ids = list(spec.children_ids)
            result.lifecycle_events = lifecycle_events
            if result.status == "completed":
                self._emit_lifecycle_event(
                    lifecycle_events,
                    "subagent/complete",
                    spec,
                    {"iterations": result.iterations, "tokens": result.total_tokens},
                )
            else:
                self._emit_lifecycle_event(
                    lifecycle_events,
                    "subagent/error",
                    spec,
                    {"status": result.status, "error": result.error},
                )
            return result
        except (asyncio.TimeoutError, TimeoutError):
            self._emit_lifecycle_event(
                lifecycle_events,
                "subagent/error",
                spec,
                {
                    "error": f"TimeoutError: Sub-agent execution exceeded {spec.timeout_seconds}s limit."
                },
            )
            return SubAgentResult(
                task_id=spec.task_id,
                session_id=session_id,
                status="timeout",
                summary=f"Sub-agent timed out after {spec.timeout_seconds:.1f} seconds.",
                error=f"TimeoutError: Sub-agent execution exceeded {spec.timeout_seconds}s limit.",
                exit_code=124,
                parent_agent_id=spec.parent_agent_id,
                root_agent_id=spec.root_agent_id,
                depth=spec.depth,
                children_ids=list(spec.children_ids),
                lifecycle_events=lifecycle_events,
            )
        except asyncio.CancelledError:
            self._emit_lifecycle_event(
                lifecycle_events,
                "subagent/error",
                spec,
                {"error": "CancelledError: Parent or runner cancelled sub-agent."},
            )
            return SubAgentResult(
                task_id=spec.task_id,
                session_id=session_id,
                status="interrupted",
                summary="Sub-agent was cancelled.",
                error="CancelledError: Parent or runner cancelled sub-agent.",
                exit_code=130,
                parent_agent_id=spec.parent_agent_id,
                root_agent_id=spec.root_agent_id,
                depth=spec.depth,
                children_ids=list(spec.children_ids),
                lifecycle_events=lifecycle_events,
            )
        except Exception as e:
            logger.exception("Sub-agent execution error")
            self._emit_lifecycle_event(
                lifecycle_events,
                "subagent/error",
                spec,
                {"error": str(e)},
            )
            return SubAgentResult(
                task_id=spec.task_id,
                session_id=session_id,
                status="failed",
                summary=f"Sub-agent encountered an error: {e}",
                error=str(e),
                exit_code=1,
                parent_agent_id=spec.parent_agent_id,
                root_agent_id=spec.root_agent_id,
                depth=spec.depth,
                children_ids=list(spec.children_ids),
                lifecycle_events=lifecycle_events,
            )
        finally:
            self._active_controllers.pop(session_id, None)
            _global_active_controllers.pop(session_id, None)
            clear_session_state(session_id)

    async def run_parallel_subagents(
        self,
        specs: list[SubAgentSpec],
        max_concurrency: int = 4,
    ) -> list[SubAgentResult]:
        """Concurrently execute multiple sub-agents with bounded concurrency."""
        if not specs:
            return []

        semaphore = asyncio.Semaphore(max_concurrency)

        async def _bounded_run(spec: SubAgentSpec) -> SubAgentResult:
            async with semaphore:
                return await self.spawn_subagent(spec)

        tasks = [_bounded_run(s) for s in specs]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        final_results: list[SubAgentResult] = []
        for i, res in enumerate(results):
            if isinstance(res, BaseException):
                final_results.append(
                    SubAgentResult(
                        task_id=specs[i].task_id,
                        session_id=f"sub_{specs[i].task_id}",
                        status="failed",
                        summary=f"Sub-agent failed with exception: {res}",
                        error=str(res),
                    )
                )
            elif isinstance(res, SubAgentResult):
                final_results.append(res)

        return final_results

    async def run_continuable(self, spec: SubAgentSpec, session_id: str) -> SubAgentResult:
        """Drive a parked continuable worker: turn → settle-notice → park → repeat.

        Mirrors the harness continuable-Activation semantics on CoderAI's
        in-memory primitives: one durable conversation, FIFO turns queued
        through the handle inbox, current-turn-only interruption (queued
        messages stay parked), and a one-shot settlement notice when a turn
        finishes with no queued follow-up work.
        """
        from coderai.core.orchestration import settlement_summary

        handle = spec.handle
        if handle is None:
            return await self._spawn_subagent_inner(spec, session_id)

        state: dict[str, Any] = {}
        abort_event = asyncio.Event()
        self._active_controllers[session_id] = abort_event
        last_result: SubAgentResult | None = None
        try:
            while True:
                abort_event.clear()
                waiter = getattr(handle, "inbox_waiter", None)
                if waiter is not None:
                    waiter.clear()
                try:
                    result = await asyncio.wait_for(
                        self._run_subagent_loop(
                            spec,
                            session_id,
                            abort_event,
                            lifecycle_events=None,
                            continuable=True,
                            messages=state.get("messages"),
                            state=state,
                        ),
                        timeout=spec.timeout_seconds,
                    )
                except asyncio.CancelledError:
                    result = SubAgentResult(
                        task_id=spec.task_id,
                        session_id=session_id,
                        status="interrupted",
                        summary="Sub-agent was cancelled.",
                        error="CancelledError: Parent or runner cancelled sub-agent.",
                        exit_code=130,
                        parent_agent_id=spec.parent_agent_id,
                        root_agent_id=spec.root_agent_id,
                        depth=spec.depth,
                        children_ids=list(spec.children_ids),
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    result = SubAgentResult(
                        task_id=spec.task_id,
                        session_id=session_id,
                        status="timeout",
                        summary=f"Sub-agent timed out after {spec.timeout_seconds:.1f} seconds.",
                        error=f"TimeoutError: Sub-agent execution exceeded {spec.timeout_seconds}s limit.",
                        exit_code=124,
                        parent_agent_id=spec.parent_agent_id,
                        root_agent_id=spec.root_agent_id,
                        depth=spec.depth,
                        children_ids=list(spec.children_ids),
                    )
                last_result = result
                handle.result = result
                if handle.status != "interrupted":
                    handle.status = result.status
                handle.last_stop_reason = result.stop_reason

                # One-shot settlement notice when a turn finishes with no
                # queued follow-up work (harness subagent-settled notice).
                if (
                    result.status == "completed"
                    and not getattr(handle, "settled_notified", False)
                    and not (getattr(handle, "inbox", None) or [])
                ):
                    notice = getattr(handle, "parent_notice", None)
                    if notice is not None:
                        try:
                            outcome_text = handle.report or result.summary
                            notice(
                                spec.parent_session_id,
                                settlement_summary(handle.id, "completed", outcome=outcome_text),
                            )
                        except Exception:
                            logger.debug("continuable settlement notice failed", exc_info=True)
                    handle.settled_notified = True

                # Park until send_message wakes us or the handle is killed.
                waiter = getattr(handle, "inbox_waiter", None)
                if waiter is None:
                    return result
                await waiter.wait()
        finally:
            self._active_controllers.pop(session_id, None)
            clear_session_state(session_id)
        return last_result or SubAgentResult(
            task_id=spec.task_id,
            session_id=session_id,
            status="interrupted",
            summary="Sub-agent was cancelled.",
            error="CancelledError: Parent or runner cancelled sub-agent.",
            exit_code=130,
            parent_agent_id=spec.parent_agent_id,
            root_agent_id=spec.root_agent_id,
            depth=spec.depth,
            children_ids=list(spec.children_ids),
        )

    async def _run_subagent_loop(
        self,
        spec: SubAgentSpec,
        session_id: str,
        abort_event: asyncio.Event,
        lifecycle_events: list[dict[str, Any]] | None = None,
        *,
        continuable: bool = False,
        messages: list[dict[str, Any]] | None = None,
        state: dict[str, Any] | None = None,
    ) -> SubAgentResult:
        """Run the isolated agentic loop for the sub-agent.

        ``continuable=True`` keeps the run alive across turns: the supplied
        ``messages`` conversation is reused, the terminal result is parked
        (the caller owns re-entry), and the conversation is stashed on
        ``state["messages"]`` for the next turn.
        """
        events = lifecycle_events if lifecycle_events is not None else []
        client_info = self.create_openai_client()
        client = client_info.get("client")
        model = str(client_info.get("model") or "gpt-5.6-luna")
        base_url = client_info.get("baseURL")
        temperature = client_info.get("temperature")
        thinking_enabled = bool(client_info.get("thinkingEnabled"))
        reasoning_effort = client_info.get("reasoningEffort") or "max"

        if client is None:
            return SubAgentResult(
                task_id=spec.task_id,
                session_id=session_id,
                status="failed",
                summary="API key not found for sub-agent execution.",
                error="AuthenticationError: Missing API client.",
                parent_agent_id=spec.parent_agent_id,
                root_agent_id=spec.root_agent_id,
                depth=spec.depth,
                children_ids=list(spec.children_ids),
                lifecycle_events=events,
            )

        # Setup sandboxed tools
        from coderai.core.tools.executor import ToolExecutor

        effective_root = spec.isolated_cwd or self.project_root
        tool_executor = ToolExecutor(effective_root, self.create_openai_client)
        available_tools = self._get_sandboxed_tools(spec, model)

        if messages is None:
            # Build isolated initial message history with static system prompt
            system_prompt = get_subagent_system_prompt(spec.mode)
            runtime_context = get_runtime_context(effective_root, model)

            initial_user_prompt = spec.prompt
            if spec.description:
                initial_user_prompt = f"Goal: {spec.description}\n\n{initial_user_prompt}"
            if spec.extra_context:
                initial_user_prompt += f"\n\nAdditional Context:\n{spec.extra_context}"
            if runtime_context:
                initial_user_prompt = f"{runtime_context}\n\n---\n\n{initial_user_prompt}"

            messages = [
                {"role": "system", "content": system_prompt},
            ]
            if spec.seed_messages:
                for sm in spec.seed_messages:
                    role = sm.get("role")
                    if role and role != "system":
                        messages.append(dict(sm))

            messages.append({"role": "user", "content": initial_user_prompt})

        if state is not None:
            state["messages"] = messages

        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_cached_tokens = 0
        active_tokens = 0
        tool_calls_count = 0
        last_assistant_reply = ""
        artifacts: list[str] = []
        diffs: list[dict[str, Any]] = []

        for iteration in range(1, spec.max_iterations + 1):
            if abort_event.is_set():
                return SubAgentResult(
                    task_id=spec.task_id,
                    session_id=session_id,
                    status="interrupted",
                    summary=last_assistant_reply or "Sub-agent was interrupted.",
                    active_tokens=active_tokens,
                    total_tokens=total_prompt_tokens + total_completion_tokens,
                    cached_tokens=total_cached_tokens,
                    iterations=iteration,
                    tool_calls_count=tool_calls_count,
                    artifacts=artifacts,
                    diffs=diffs,
                    exit_code=130,
                    token_telemetry={
                        "prompt_tokens": total_prompt_tokens,
                        "completion_tokens": total_completion_tokens,
                        "cached_tokens": total_cached_tokens,
                        "active_tokens": active_tokens,
                        "total_tokens": total_prompt_tokens + total_completion_tokens,
                    },
                    parent_agent_id=spec.parent_agent_id,
                    root_agent_id=spec.root_agent_id,
                    depth=spec.depth,
                    children_ids=list(spec.children_ids),
                    lifecycle_events=events,
                )

            # Check for queued steering / interactive messages from parent/user
            inbox_messages: list[str] = []
            if spec.handle and getattr(spec.handle, "inbox", None):
                while spec.handle.inbox:
                    inbox_messages.append(spec.handle.inbox.pop(0))
            elif spec.agent_id or spec.task_id:
                from coderai.core.agents import get_agent_registry

                reg_handle = (
                    get_agent_registry().get(spec.agent_id or "")
                    or get_agent_registry().get(spec.task_id or "")
                    or get_agent_registry().get(f"agent_{spec.task_id}")
                )
                if reg_handle and reg_handle.inbox:
                    while reg_handle.inbox:
                        inbox_messages.append(reg_handle.inbox.pop(0))

            if inbox_messages:
                steering_text = "\n\n".join(
                    [f"[Steering from parent]: {msg}" for msg in inbox_messages]
                )
                messages.append({"role": "user", "content": steering_text})

            # Build request
            request: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "tools": available_tools if available_tools else None,
            }
            if temperature is not None:
                request["temperature"] = temperature
            if spec.max_tokens is not None:
                request["max_tokens"] = spec.max_tokens
            request.update(
                build_thinking_request_options(
                    thinking_enabled,
                    base_url=base_url,
                    reasoning_effort=reasoning_effort,
                    model=model,
                    has_tools=bool(available_tools),
                )
            )
            if not request.get("tools"):
                request.pop("tools", None)

            # LLM invocation with retryable-failure backoff (harness retry
            # policy: 429/5xx/timeout/transport retried with jittered backoff)
            from coderai.core.common.llm_retry import (
                classify_llm_failure,
                provider_retry_after_ms,
                retry_delay_ms,
            )

            response: dict[str, Any] | None = None
            last_error: Exception | None = None
            for _attempt in range(1, 6):
                if abort_event.is_set():
                    break
                try:
                    response = await asyncio.to_thread(_call_llm_sync, client, request)
                    break
                except (asyncio.CancelledError, TimeoutError):
                    raise
                except Exception as e:
                    last_error = e
                    if classify_llm_failure(e) is None or _attempt >= 5:
                        break
                    provider_ms = provider_retry_after_ms(e)
                    if provider_ms is not None and provider_ms / 1000.0 > 10.0:
                        break  # over-cap Retry-After gives up in normal mode
                    delay = max(
                        retry_delay_ms(_attempt) / 1000.0,
                        (provider_ms or 0.0) / 1000.0,
                    )
                    try:
                        await asyncio.sleep(delay)
                    except asyncio.CancelledError:
                        raise

            if abort_event.is_set():
                return SubAgentResult(
                    task_id=spec.task_id,
                    session_id=session_id,
                    status="interrupted",
                    summary=last_assistant_reply or "Sub-agent was interrupted.",
                    active_tokens=active_tokens,
                    total_tokens=total_prompt_tokens + total_completion_tokens,
                    cached_tokens=total_cached_tokens,
                    iterations=iteration,
                    tool_calls_count=tool_calls_count,
                    artifacts=artifacts,
                    diffs=diffs,
                    exit_code=130,
                    token_telemetry={
                        "prompt_tokens": total_prompt_tokens,
                        "completion_tokens": total_completion_tokens,
                        "cached_tokens": total_cached_tokens,
                        "active_tokens": active_tokens,
                        "total_tokens": total_prompt_tokens + total_completion_tokens,
                    },
                    parent_agent_id=spec.parent_agent_id,
                    root_agent_id=spec.root_agent_id,
                    depth=spec.depth,
                    children_ids=list(spec.children_ids),
                    lifecycle_events=events,
                )
            if response is None:
                return SubAgentResult(
                    task_id=spec.task_id,
                    session_id=session_id,
                    status="failed",
                    summary=f"LLM request error in sub-agent: {last_error}",
                    active_tokens=active_tokens,
                    total_tokens=total_prompt_tokens + total_completion_tokens,
                    cached_tokens=total_cached_tokens,
                    iterations=iteration,
                    tool_calls_count=tool_calls_count,
                    error=str(last_error),
                    exit_code=1,
                    token_telemetry={
                        "prompt_tokens": total_prompt_tokens,
                        "completion_tokens": total_completion_tokens,
                        "cached_tokens": total_cached_tokens,
                        "active_tokens": active_tokens,
                        "total_tokens": total_prompt_tokens + total_completion_tokens,
                    },
                    artifacts=artifacts,
                    diffs=diffs,
                    parent_agent_id=spec.parent_agent_id,
                    root_agent_id=spec.root_agent_id,
                    depth=spec.depth,
                    children_ids=list(spec.children_ids),
                    lifecycle_events=events,
                )

            usage = response.get("usage") or {}
            p_tok = usage.get("prompt_tokens", 0)
            c_tok = usage.get("completion_tokens", 0)
            cached_tok = usage.get("cached_tokens", 0) or usage.get("prompt_cache_hit_tokens", 0)
            total_prompt_tokens += p_tok
            total_completion_tokens += c_tok
            total_cached_tokens += cached_tok
            active_tokens = usage.get("total_tokens", p_tok + c_tok)

            if (
                spec.token_budget is not None
                and (total_prompt_tokens + total_completion_tokens) >= spec.token_budget
            ):
                return SubAgentResult(
                    task_id=spec.task_id,
                    session_id=session_id,
                    status="budget_exceeded",
                    summary=last_assistant_reply
                    or f"Sub-agent reached token budget cap ({spec.token_budget} tokens).",
                    active_tokens=active_tokens,
                    total_tokens=total_prompt_tokens + total_completion_tokens,
                    cached_tokens=total_cached_tokens,
                    iterations=iteration,
                    tool_calls_count=tool_calls_count,
                    exit_code=2,
                    token_telemetry={
                        "prompt_tokens": total_prompt_tokens,
                        "completion_tokens": total_completion_tokens,
                        "cached_tokens": total_cached_tokens,
                        "active_tokens": active_tokens,
                        "total_tokens": total_prompt_tokens + total_completion_tokens,
                    },
                    artifacts=artifacts,
                    diffs=diffs,
                    parent_agent_id=spec.parent_agent_id,
                    root_agent_id=spec.root_agent_id,
                    depth=spec.depth,
                    children_ids=list(spec.children_ids),
                    lifecycle_events=events,
                )

            choice = (response.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            content = msg.get("content") or ""
            raw_tool_calls = msg.get("tool_calls")
            thinking = msg.get("reasoning_content")
            refusal = msg.get("refusal")

            if content:
                last_assistant_reply = content

            if refusal:
                return SubAgentResult(
                    task_id=spec.task_id,
                    session_id=session_id,
                    status="refusal",
                    summary=f"Model refused request: {refusal}",
                    active_tokens=active_tokens,
                    total_tokens=total_prompt_tokens + total_completion_tokens,
                    cached_tokens=total_cached_tokens,
                    iterations=iteration,
                    tool_calls_count=tool_calls_count,
                    error=refusal,
                    exit_code=1,
                    token_telemetry={
                        "prompt_tokens": total_prompt_tokens,
                        "completion_tokens": total_completion_tokens,
                        "cached_tokens": total_cached_tokens,
                        "active_tokens": active_tokens,
                        "total_tokens": total_prompt_tokens + total_completion_tokens,
                    },
                    artifacts=artifacts,
                    diffs=diffs,
                    parent_agent_id=spec.parent_agent_id,
                    root_agent_id=spec.root_agent_id,
                    depth=spec.depth,
                    children_ids=list(spec.children_ids),
                    lifecycle_events=events,
                )

            tool_calls = _normalize_subagent_tool_calls(raw_tool_calls)

            # Record assistant turn
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": content,
            }
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            if thinking:
                assistant_msg["reasoning_content"] = thinking
            messages.append(assistant_msg)

            if not tool_calls:
                # Agent concluded with final response
                return SubAgentResult(
                    task_id=spec.task_id,
                    session_id=session_id,
                    status="completed",
                    summary=content or "Sub-agent completed task without text output.",
                    active_tokens=active_tokens,
                    total_tokens=total_prompt_tokens + total_completion_tokens,
                    cached_tokens=total_cached_tokens,
                    iterations=iteration,
                    tool_calls_count=tool_calls_count,
                    exit_code=0,
                    token_telemetry={
                        "prompt_tokens": total_prompt_tokens,
                        "completion_tokens": total_completion_tokens,
                        "cached_tokens": total_cached_tokens,
                        "active_tokens": active_tokens,
                        "total_tokens": total_prompt_tokens + total_completion_tokens,
                    },
                    artifacts=artifacts,
                    diffs=diffs,
                    parent_agent_id=spec.parent_agent_id,
                    root_agent_id=spec.root_agent_id,
                    depth=spec.depth,
                    children_ids=list(spec.children_ids),
                    lifecycle_events=events,
                )

            # Execute tools in sandbox
            tool_calls_count += len(tool_calls)
            for tc in tool_calls:
                if abort_event.is_set():
                    break

                fn_name = tc.get("function", {}).get("name", "")
                fn_args_raw = tc.get("function", {}).get("arguments", "{}")

                # Track examined artifacts/files
                try:
                    parsed_args = (
                        json.loads(fn_args_raw) if isinstance(fn_args_raw, str) else fn_args_raw
                    )
                    fp = parsed_args.get("file_path") or parsed_args.get("path")
                    if fp and isinstance(fp, str) and fp not in artifacts:
                        artifacts.append(fp)
                except Exception:
                    pass

                # Check tool permissions inside sub-agent
                if spec.mode == "read_only" and fn_name in ("write", "Write", "edit", "Edit"):
                    tool_result_content = json.dumps(
                        {
                            "ok": False,
                            "name": fn_name,
                            "error": f"PermissionDenied: Sub-agent in read_only mode cannot invoke mutating tool '{fn_name}'.",
                        }
                    )
                else:
                    hooks: ToolExecutionHooks = ToolExecutionHooks(
                        should_stop=lambda: abort_event.is_set(),
                        isolated_cwd=spec.isolated_cwd or spec.scratchpad_dir,
                        dry_run=spec.dry_run,
                        on_after_file_mutation=lambda fp: diffs.append({"file_path": str(fp)}),
                    )
                    executions = await tool_executor.execute_tool_calls(
                        session_id, [tc], hooks=hooks
                    )

                    if executions:
                        tool_result_content = executions[0]["content"]
                    else:
                        tool_result_content = json.dumps(
                            {"ok": False, "error": "Execution aborted."}
                        )

                messages.append(
                    {
                        "role": "tool",
                        "content": tool_result_content or "(no output)",
                        "tool_call_id": tc.get("id", ""),
                    }
                )

        # Exceeded max iterations
        return SubAgentResult(
            task_id=spec.task_id,
            session_id=session_id,
            status="max_iterations",
            summary=last_assistant_reply
            or "Sub-agent reached max iteration limit before final conclusion.",
            active_tokens=active_tokens,
            total_tokens=total_prompt_tokens + total_completion_tokens,
            cached_tokens=total_cached_tokens,
            iterations=spec.max_iterations,
            tool_calls_count=tool_calls_count,
            exit_code=2,
            token_telemetry={
                "prompt_tokens": total_prompt_tokens,
                "completion_tokens": total_completion_tokens,
                "cached_tokens": total_cached_tokens,
                "active_tokens": active_tokens,
                "total_tokens": total_prompt_tokens + total_completion_tokens,
            },
            artifacts=artifacts,
            diffs=diffs,
            parent_agent_id=spec.parent_agent_id,
            root_agent_id=spec.root_agent_id,
            depth=spec.depth,
            children_ids=list(spec.children_ids),
            lifecycle_events=events,
        )

    def _get_sandboxed_tools(self, spec: SubAgentSpec, model: str) -> list[dict[str, Any]]:
        """Filter tools for subagent execution with deterministic ordering and canonicalization."""
        from coderai.core.prompt import format_tool_definitions
        from coderai.core.prompt_sections import order_tools
        from coderai.core.tools.types import canonicalize_tool_schema

        all_tools = get_tools({"model": model, "nonInteractive": True, "childAgent": True})

        filtered: list[dict[str, Any]] = []
        for tool in all_tools:
            name = tool.get("function", {}).get("name", "")

            # Sub-agents never ask interactive user questions
            if name == "AskUserQuestion":
                continue

            # Limit sub-agent recursion
            if name in ("Task", "subagent", "subagent_fork") and spec.depth >= MAX_SUBAGENT_DEPTH:
                continue

            # Read-only mode disallows mutating tools
            if spec.mode == "read_only" and name in ("write", "Write", "edit", "Edit"):
                continue

            if spec.allowed_tools and name not in spec.allowed_tools:
                continue

            if (
                spec.descriptor
                and spec.descriptor.tool_filter
                and not spec.descriptor.tool_filter.is_tool_permitted(name)
            ):
                continue

            filtered.append(tool)

        formatted = format_tool_definitions(filtered, model=model)
        return order_tools([canonicalize_tool_schema(t) for t in formatted])


def _normalize_subagent_tool_calls(raw: Any) -> list[dict[str, Any]] | None:
    if not raw:
        return None
    result: list[dict[str, Any]] = []
    for tc in raw:
        if isinstance(tc, dict):
            func = tc.get("function") or {}
            tc_id = tc.get("id") or uuid.uuid4().hex
            result.append(
                {
                    "id": tc_id,
                    "type": "function",
                    "function": {
                        "name": func.get("name", ""),
                        "arguments": func.get("arguments", "") or "",
                    },
                }
            )
        else:
            tc_id = getattr(tc, "id", "") or uuid.uuid4().hex
            result.append(
                {
                    "id": tc_id,
                    "type": "function",
                    "function": {
                        "name": getattr(getattr(tc, "function", None), "name", "") or "",
                        "arguments": getattr(getattr(tc, "function", None), "arguments", "") or "",
                    },
                }
            )
    return result or None


def _call_llm_sync(client: Any, request: dict[str, Any]) -> dict[str, Any]:
    """Synchronous LLM call wrapper supporting both OpenAI SDK objects and dict responses."""
    resp = client.chat.completions.create(**request)
    if isinstance(resp, dict):
        choices = resp.get("choices") or [{}]
        choice = choices[0] if choices else {}
        msg = choice.get("message") or {}
        dict_res: dict[str, Any] = {
            "choices": [
                {
                    "message": {
                        "content": msg.get("content") or "",
                        "tool_calls": msg.get("tool_calls"),
                        "reasoning_content": msg.get("reasoning_content"),
                        "refusal": msg.get("refusal"),
                    }
                }
            ]
        }
        usage = resp.get("usage")
        if usage:
            dict_res["usage"] = extract_usage_dict(usage)
        return dict_res

    choice = resp.choices[0]
    msg = choice.message
    tool_calls = None
    raw_tc = getattr(msg, "tool_calls", None)
    if raw_tc:
        tool_calls = []
        for tc in raw_tc:
            func = getattr(tc, "function", None)
            tool_calls.append(
                {
                    "id": getattr(tc, "id", "") or uuid.uuid4().hex,
                    "type": "function",
                    "function": {
                        "name": getattr(func, "name", "") or "",
                        "arguments": getattr(func, "arguments", "") or "",
                    },
                }
            )

    res: dict[str, Any] = {
        "choices": [
            {
                "message": {
                    "content": getattr(msg, "content", None) or "",
                    "tool_calls": tool_calls,
                    "reasoning_content": getattr(msg, "reasoning_content", None),
                    "refusal": getattr(msg, "refusal", None),
                }
            }
        ]
    }
    usage_attr = getattr(resp, "usage", None)
    if usage_attr:
        res["usage"] = extract_usage_dict(usage_attr)
    return res
