"""Sub-Agent Architecture & Engine for CoderAI.

Provides reliable sub-agent spawning, context isolation, tool/permission sandboxing,
parallel execution, cancellation/timeout recovery, and result aggregation.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import pathlib
import uuid
from typing import Any
from types import SimpleNamespace
from collections.abc import Callable

from coderai.utils.common.message_converter import OpenAIMessageConverter
from coderai.utils.common.openai_thinking import build_thinking_request_options
from coderai.orchestration import (
    publish_subagent_end,
    publish_subagent_start,
)
from coderai.prompt import get_runtime_context, get_subagent_system_prompt, get_tools
from coderai.subagents.builder import (
    check_subagent_depth_quota,
)
from coderai.file_snippets import clear_session_state
from coderai.subagents.execution import (
    build_child_execution_hooks,
    subagent_tool_denial,
    writable_child_denial,
)

logger = logging.getLogger(__name__)
from coderai.subagents.builder import (
    MAX_SUBAGENT_DEPTH,
    SubAgentSpec,
    build_spec as build_spec,
)
from coderai.subagents.core import _call_llm_sync, _normalize_subagent_tool_calls
from coderai.subagents.output import SubAgentResult

_global_active_controllers: dict[str, asyncio.Event] = {}


class _TurnAborted(asyncio.CancelledError):
    """The child's abort signal fired; parent cancellation remains distinct."""


def _partial_summary(spec: SubAgentSpec, fallback: str) -> str:
    for message in reversed(getattr(spec.handle, "conversation", None) or []):
        if message.get("role") == "assistant" and message.get("content"):
            return str(message["content"])
    return fallback


async def _run_abortable(operation: Any, abort_event: asyncio.Event, timeout: float) -> Any:
    """Link a turn's abort signal to its complete provider/tool execution.

    Checking the flag only between steps leaves a child stuck in a provider
    request or a long-running tool. Always cancel and drain the owned task
    before returning, including when the parent task itself is cancelled.
    """
    task = asyncio.ensure_future(operation)
    stopped = asyncio.create_task(abort_event.wait())
    try:
        done, _ = await asyncio.wait(
            (task, stopped), timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if abort_event.is_set():
            raise _TurnAborted
        if task in done:
            return await task
        raise TimeoutError
    finally:
        if not task.done():
            abort_event.set()
        for pending in (task, stopped):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(task, stopped, return_exceptions=True)


def make_subagent_result(
    spec: SubAgentSpec,
    session_id: str,
    status: str,
    summary: str = "",
    *,
    error: str | None = None,
    exit_code: int = 0,
    active_tokens: int = 0,
    total_tokens: int = 0,
    total_prompt_tokens: int = 0,
    total_completion_tokens: int = 0,
    total_cached_tokens: int = 0,
    iteration: int = 0,
    tool_calls_count: int = 0,
    artifacts: list[str] | None = None,
    diffs: list[Any] | None = None,
    lifecycle_events: list[Any] | None = None,
    duration_seconds: float = 0.0,
) -> SubAgentResult:
    """Consolidated builder for SubAgentResult instances (WF-D4)."""
    if total_tokens == 0 and (total_prompt_tokens > 0 or total_completion_tokens > 0):
        total_tokens = total_prompt_tokens + total_completion_tokens
    elif total_tokens > 0 and total_prompt_tokens == 0 and total_completion_tokens == 0:
        total_prompt_tokens = total_tokens
    token_telemetry = (
        {
            "prompt_tokens": total_prompt_tokens,
            "completion_tokens": total_completion_tokens,
            "cached_tokens": total_cached_tokens,
            "active_tokens": active_tokens,
            "total_tokens": total_tokens,
        }
        if (total_tokens > 0 or active_tokens > 0)
        else None
    )

    return SubAgentResult(
        task_id=spec.task_id,
        session_id=session_id,
        status=status,
        summary=summary,
        error=error,
        exit_code=exit_code,
        duration_seconds=duration_seconds,
        active_tokens=active_tokens,
        total_tokens=total_tokens,
        cached_tokens=total_cached_tokens,
        iterations=iteration,
        tool_calls_count=tool_calls_count,
        artifacts=artifacts or [],
        diffs=diffs or [],
        token_telemetry=token_telemetry or {},
        parent_agent_id=spec.parent_agent_id,
        root_agent_id=spec.root_agent_id,
        depth=spec.depth,
        children_ids=list(spec.children_ids),
        lifecycle_events=lifecycle_events or [],
    )


class SubAgentManager:
    """Manages sub-agent lifecycle, sandboxed execution, and parallel concurrency."""

    def __init__(
        self,
        project_root: str,
        create_openai_client: Callable[[], dict[str, Any]] | None = None,
        get_resolved_settings: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.project_root = str(pathlib.Path(project_root).resolve())
        from coderai.llm import create_oauth_manager

        self.oauth_manager = create_oauth_manager(self.project_root)
        if create_openai_client is None:
            from coderai.llm import create_openai_client as _default_create_client

            self.create_openai_client = lambda: _default_create_client(
                self.project_root, oauth=self.oauth_manager
            )
        else:
            self.create_openai_client = create_openai_client
        self.get_resolved_settings = get_resolved_settings or (lambda: {})
        self._inherits_permission_settings = get_resolved_settings is not None
        self.message_converter = OpenAIMessageConverter()
        self._active_controllers: dict[str, asyncio.Event] = {}

    def _resolve_subagent_cwd(self, spec: SubAgentSpec, session_id: str) -> str | None:
        if spec.isolated_cwd:
            return spec.isolated_cwd

        return None

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
            del spec.handle.lifecycle_history[:-200]
        return evt

    def cancel_subagent(self, session_id: str) -> None:
        """Abort a current turn and its descendants across manager scopes."""
        from coderai.subagents.core import get_agent_registry

        registry = get_agent_registry()
        session_ids = {session_id}
        for handle in registry.list():
            if handle.run_session_id == session_id:
                session_ids.update(
                    child.run_session_id
                    for child in registry.list_descendants(handle.id)
                    if child.run_session_id
                )
        for child_session in session_ids:
            event = self._active_controllers.get(child_session) or _global_active_controllers.get(
                child_session
            )
            if event is not None:
                event.set()

    def cancel_all(self) -> None:
        """Cancel only the sub-agents owned by this manager."""
        for event in list(self._active_controllers.values()):
            event.set()

    def _prepare_subagent_store(
        self, spec: SubAgentSpec, agent_id: str, *, background: bool = False
    ) -> Any:
        from coderai.subagents.store import SubagentStore
        from coderai.soul.session.store import JsonlSessionStore

        try:
            parent_sid = spec.parent_session_id or "root"
            if spec.session_manager is not None and hasattr(spec.session_manager, "_session_dir"):
                directory = spec.session_manager._session_dir(parent_sid)
            else:
                from coderai.utils.storage import owned_path

                directory = owned_path(
                    JsonlSessionStore(self.project_root, cleanup=False).project_dir, parent_sid
                )
            store = SubagentStore(directory)
            store.create_instance(
                agent_id=agent_id,
                subagent_type=spec.subagent_type or "default",
                description=spec.description,
                model_override=spec.model,
            )
            store.write_prompt(agent_id, spec.prompt)
            store.update_instance(
                agent_id,
                status="running_background" if background else "running_foreground",
                last_task_id=spec.task_id,
            )
            spec.checkpoint_store = store
            return store
        except OSError:
            logger.warning("Could not initialize durable child context for %s", agent_id)
            raise

    def _save_subagent_checkpoint(self, spec: SubAgentSpec, messages: list[dict[str, Any]]) -> None:
        if spec.checkpoint_store is not None:
            try:
                spec.checkpoint_store.write_context(spec.agent_id or spec.task_id, messages)
            except OSError:
                logger.warning("Could not persist child checkpoint for %s", spec.task_id)

    def _save_subagent_result(self, spec: SubAgentSpec, result: SubAgentResult) -> None:
        if spec.checkpoint_store is not None:
            status = (
                "completed"
                if result.status == "completed"
                else "killed"
                if result.status == "interrupted"
                else "failed"
            )
            try:
                spec.checkpoint_store.write_output(spec.agent_id or spec.task_id, result.summary)
                spec.checkpoint_store.update_instance(spec.agent_id or spec.task_id, status=status)
            except OSError:
                logger.warning("Could not persist child result for %s", spec.task_id)

    async def spawn_subagent(self, spec: SubAgentSpec) -> SubAgentResult:
        """Spawn and execute a single isolated sub-agent with timeout, quota checks, and error recovery.

        Publishes the harness ``subagent/start`` + ``subagent/end`` lifecycle
        pair around the run. A start-time quota rejection fails before
        publication (mirroring the reference's pre-publication rejection), so
        no lifecycle edge pair is emitted for a child that never existed.
        """
        from coderai.subagents.core import get_agent_registry

        parent = get_agent_registry().get(spec.parent_agent_id) if spec.parent_agent_id else None
        if parent is not None and (parent.killed or parent.status == "interrupted"):
            raise ValueError("Cannot spawn from an interrupted parent agent")
        if spec.project_root is None:
            spec.project_root = self.project_root
        session_id = (
            f"sub_{spec.parent_session_id[:8] if spec.parent_session_id else 'root'}_{spec.task_id}"
        )
        if not check_subagent_depth_quota(spec.depth, spec.max_depth)[0]:
            return await self._spawn_subagent_inner(spec, session_id)
        from coderai.subagents.core import AgentHandle, get_agent_registry

        if spec.root_agent_id is None:
            spec.root_agent_id = spec.agent_id or spec.task_id
        handle = AgentHandle(
            id=spec.task_id,
            parent_session_id=spec.parent_session_id or "",
            run_session_id=session_id,
            description=spec.description,
            mode=spec.mode or "read_only",
            depth=spec.depth,
            parent_agent_id=spec.parent_agent_id,
            root_agent_id=spec.root_agent_id,
            spec=spec,
            manager=self,
            task=asyncio.current_task(),
        )
        spec.handle = handle
        get_agent_registry().register(handle)
        if spec.parent_agent_id:
            parent_handle = get_agent_registry().get(spec.parent_agent_id)
            if parent_handle is not None and handle.id not in parent_handle.children_ids:
                parent_handle.children_ids.append(handle.id)

        provider = spec.provider or "in_process"
        local = provider == "in_process"
        run_id = uuid.uuid4().hex
        from coderai.wire.emitter import bind_emitter, get_emitter, reset_emitter
        from coderai.subagents.streaming import ChildWireEmitter

        child_emitter: ChildWireEmitter | None = None
        emitter_token = None
        started = False
        result: SubAgentResult | None = None
        try:
            self._prepare_subagent_store(spec, handle.id)
            child_emitter = ChildWireEmitter(
                get_emitter(),
                handle.id,
                spec.subagent_type,
                spec.parent_tool_call_id,
                spec.parent_session_id,
                wire_path=(
                    spec.checkpoint_store.instance_dir(handle.id) / "wire.jsonl"
                    if spec.checkpoint_store is not None
                    else None
                ),
            )
            publish_subagent_start(
                run_id=run_id,
                provider=provider,
                child_id=session_id,
                local=local,
                parent_session_id=spec.parent_session_id,
            )
            started = True
            emitter_token = bind_emitter(child_emitter)
            result = await self._spawn_subagent_inner(spec, session_id)
        except asyncio.CancelledError:
            result = make_subagent_result(
                spec,
                session_id,
                "interrupted",
                summary=_partial_summary(spec, "Sub-agent was cancelled."),
                exit_code=130,
            )
            raise
        except Exception as exc:
            result = make_subagent_result(
                spec,
                session_id,
                "failed",
                summary="Sub-agent startup or execution failed.",
                error=str(exc),
                exit_code=1,
            )
            raise
        finally:
            if result is not None:
                handle.result = result
                handle.status = result.status
                handle.last_stop_reason = result.stop_reason
                get_agent_registry().changed()
                self._save_subagent_result(spec, result)
                if result.status in ("interrupted", "timeout", "failed"):
                    self.cancel_subagent(session_id)
            # The invoking task may keep working after this child settles;
            # retaining it would let later registry cleanup cancel its owner.
            handle.task = None
            try:
                if child_emitter is not None:
                    child_emitter.close()
            finally:
                if emitter_token is not None:
                    reset_emitter(emitter_token)
            if started:
                publish_subagent_end(
                    run_id=run_id,
                    provider=provider,
                    child_id=session_id,
                    local=local,
                    stop_reason=result.stop_reason if result is not None else "error",
                    last_assistant_message=(
                        [{"type": "text", "text": result.summary}]
                        if result is not None and result.summary
                        else None
                    ),
                    parent_session_id=spec.parent_session_id,
                )
        assert result is not None
        return result

    async def _start_hooks(
        self, spec: SubAgentSpec, session_id: str, abort: asyncio.Event, deadline: float
    ) -> None:
        from coderai.hooks.runner import run_on_subagent_spawn_async, run_subagent_start_async

        parent = spec.parent_session_id or "root"
        operations = (
            lambda: run_on_subagent_spawn_async(
                parent_session_id=parent,
                subagent_id=session_id,
                task=spec.prompt or spec.description,
                mode=spec.mode or "read_only",
                project_root=self.project_root,
                settings=self.get_resolved_settings(),
            ),
            lambda: run_subagent_start_async(
                parent,
                self.project_root,
                spec.subagent_type or spec.mode or "read_only",
                spec.prompt or spec.description,
                settings=self.get_resolved_settings(),
            ),
        )
        for operation in operations:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError
            outcome = await _run_abortable(operation(), abort, remaining)
            if outcome and (outcome.stop or outcome.decision in {"deny", "ask"}):
                raise PermissionError(
                    outcome.reason or outcome.stop_reason or "Subagent launch denied by hook"
                )

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
            return make_subagent_result(
                spec,
                session_id,
                "failed",
                summary="Maximum sub-agent nesting depth exceeded.",
                error=quota_err
                or f"RecursionLimitError: Sub-agent depth {spec.depth} exceeds max_depth {effective_max_depth}.",
                exit_code=1,
                lifecycle_events=lifecycle_events,
            )

        abort_event = asyncio.Event()
        self._active_controllers[session_id] = abort_event
        _global_active_controllers[session_id] = abort_event

        self._emit_lifecycle_event(
            lifecycle_events,
            "subagent/start",
            spec,
            {"timeout_seconds": spec.timeout_seconds, "max_iterations": spec.max_iterations},
        )

        outcome_summary: str = ""

        parent_sid = spec.parent_session_id or "root"
        deadline = asyncio.get_running_loop().time() + spec.timeout_seconds
        try:
            await self._start_hooks(spec, session_id, abort_event, deadline)
            if spec.provider != "in_process" and (
                spec.mode == "read_only"
                or spec.sandbox_mode not in (None, "workspace-write")
                or spec.on_before_file_mutation is not None
                or spec.on_after_file_mutation is not None
                or spec.allowed_tools is not None
                or spec.exclude_tools
                or spec.plan_mode
                or spec.token_budget is not None
                or spec.max_tokens is not None
                or (spec.descriptor and spec.descriptor.tool_filter)
                or self._inherits_permission_settings
            ):
                raise PermissionError(
                    "External backend cannot enforce this child capability policy; use in_process"
                )
            if spec.provider == "claude_code":
                from coderai.subagents.backends.claude_code import (
                    ClaudeCodeDriver,
                    ClaudeCodeConfig,
                )

                claude_driver = ClaudeCodeDriver(
                    ClaudeCodeConfig(
                        timeout_seconds=spec.timeout_seconds,
                        cwd=spec.isolated_cwd or self.project_root,
                    )
                )
                raw_res = await _run_abortable(
                    claude_driver.execute(
                        spec.prompt, project_root=spec.isolated_cwd or self.project_root
                    ),
                    abort_event,
                    max(0.0, deadline - asyncio.get_running_loop().time()),
                )
                status = raw_res.get("status", "completed" if raw_res.get("ok") else "failed")
                result = make_subagent_result(
                    spec,
                    session_id,
                    status,
                    summary=raw_res.get("summary", ""),
                    error=raw_res.get("error"),
                    exit_code=0 if status == "completed" else 1,
                    duration_seconds=raw_res.get("duration_seconds", 0.0),
                    lifecycle_events=lifecycle_events,
                )
            elif spec.provider == "codex":
                from coderai.subagents.backends.codex import CodexDriver, CodexConfig

                codex_driver = CodexDriver(
                    CodexConfig(
                        timeout_seconds=spec.timeout_seconds,
                        cwd=spec.isolated_cwd or self.project_root,
                    )
                )
                raw_res = await _run_abortable(
                    codex_driver.execute(
                        spec.prompt, project_root=spec.isolated_cwd or self.project_root
                    ),
                    abort_event,
                    max(0.0, deadline - asyncio.get_running_loop().time()),
                )
                status = raw_res.get("status", "completed" if raw_res.get("ok") else "failed")
                result = make_subagent_result(
                    spec,
                    session_id,
                    status,
                    summary=raw_res.get("summary", ""),
                    error=raw_res.get("error"),
                    exit_code=0 if status == "completed" else 1,
                    duration_seconds=raw_res.get("duration_seconds", 0.0),
                    lifecycle_events=lifecycle_events,
                )
            elif spec.provider == "acp":
                from coderai.acp.runner import AcpSubagentRunner, AcpRunConfig

                runner = AcpSubagentRunner(
                    AcpRunConfig(
                        command="acp-agent",
                        cwd=spec.isolated_cwd or self.project_root,
                        timeout_seconds=spec.timeout_seconds,
                    )
                )
                raw_res = await _run_abortable(
                    runner.execute(spec.prompt),
                    abort_event,
                    max(0.0, deadline - asyncio.get_running_loop().time()),
                )
                status = raw_res.get("status", "completed" if raw_res.get("ok") else "failed")
                result = make_subagent_result(
                    spec,
                    session_id,
                    status,
                    summary=raw_res.get("summary", ""),
                    error=raw_res.get("error"),
                    exit_code=0 if status == "completed" else 1,
                    duration_seconds=raw_res.get("duration_seconds", 0.0),
                    lifecycle_events=lifecycle_events,
                )
            else:
                result = await _run_abortable(
                    self._run_subagent_loop(spec, session_id, abort_event, lifecycle_events),
                    abort_event,
                    max(0.0, deadline - asyncio.get_running_loop().time()),
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
            outcome_summary = result.summary or ""
            return result
        except (asyncio.TimeoutError, TimeoutError):
            outcome_summary = (
                f"TimeoutError: Sub-agent execution exceeded {spec.timeout_seconds}s limit."
            )
            self._emit_lifecycle_event(
                lifecycle_events,
                "subagent/error",
                spec,
                {"error": outcome_summary},
            )
            return make_subagent_result(
                spec,
                session_id,
                "timeout",
                summary=f"Sub-agent timed out after {spec.timeout_seconds:.1f} seconds.",
                error=outcome_summary,
                exit_code=124,
                lifecycle_events=lifecycle_events,
            )
        except asyncio.CancelledError as cancelled:
            self._emit_lifecycle_event(
                lifecycle_events,
                "subagent/error",
                spec,
                {"error": "CancelledError: Parent or runner cancelled sub-agent."},
            )
            outcome_summary = "Sub-agent was cancelled."
            if isinstance(cancelled, _TurnAborted):
                return make_subagent_result(
                    spec,
                    session_id,
                    "interrupted",
                    summary=_partial_summary(spec, "Sub-agent was cancelled."),
                    error="CancelledError: Parent or runner cancelled sub-agent.",
                    exit_code=130,
                    lifecycle_events=lifecycle_events,
                )
            raise
        except Exception as e:
            logger.exception("Sub-agent execution error")
            outcome_summary = str(e)
            self._emit_lifecycle_event(
                lifecycle_events,
                "subagent/error",
                spec,
                {"error": str(e)},
            )
            return make_subagent_result(
                spec,
                session_id,
                "failed",
                summary=f"Sub-agent encountered an error: {e}",
                error=str(e),
                exit_code=1,
                lifecycle_events=lifecycle_events,
            )
        finally:
            self._active_controllers.pop(session_id, None)
            _global_active_controllers.pop(session_id, None)
            clear_session_state(session_id)
            try:
                from coderai.hooks.runner import run_subagent_stop_async

                await asyncio.wait_for(
                    run_subagent_stop_async(
                        parent_sid,
                        self.project_root,
                        spec.subagent_type or spec.mode or "read_only",
                        outcome_summary,
                        settings=self.get_resolved_settings(),
                    ),
                    timeout=5.0,
                )
            except Exception:
                pass

    async def run_parallel_subagents(
        self,
        specs: list[SubAgentSpec],
        max_concurrency: int = 4,
    ) -> list[SubAgentResult]:
        """Concurrently execute multiple sub-agents with bounded concurrency."""
        if not specs:
            return []
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")

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
                        status="interrupted"
                        if isinstance(res, asyncio.CancelledError)
                        else "failed",
                        summary="Sub-agent was cancelled."
                        if isinstance(res, asyncio.CancelledError)
                        else f"Sub-agent failed with exception: {res}",
                        error=str(res),
                        exit_code=130 if isinstance(res, asyncio.CancelledError) else 1,
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
        from coderai.orchestration import settlement_summary
        from coderai.subagents.core import get_agent_registry

        handle = spec.handle
        if handle is None:
            return await self._spawn_subagent_inner(spec, session_id)

        state: dict[str, Any] = {}
        abort_event = asyncio.Event()
        self._active_controllers[session_id] = abort_event
        _global_active_controllers[session_id] = abort_event
        last_result: SubAgentResult | None = None
        from coderai.hooks.runner import (
            run_subagent_stop_async,
        )

        parent_sid = spec.parent_session_id or "root"
        try:
            await self._start_hooks(
                spec,
                session_id,
                abort_event,
                asyncio.get_running_loop().time() + spec.timeout_seconds,
            )
            while True:
                # Never clear a signal still observed by an old provider
                # thread: each activation owns an immutable abort lifetime.
                abort_event = asyncio.Event()
                self._active_controllers[session_id] = abort_event
                _global_active_controllers[session_id] = abort_event
                waiter = getattr(handle, "inbox_waiter", None)
                if waiter is not None:
                    waiter.clear()
                try:
                    result = await _run_abortable(
                        self._run_subagent_loop(
                            spec,
                            session_id,
                            abort_event,
                            lifecycle_events=None,
                            continuable=True,
                            messages=state.get("messages"),
                            state=state,
                        ),
                        abort_event,
                        spec.timeout_seconds,
                    )
                except asyncio.CancelledError as cancelled:
                    if isinstance(cancelled, _TurnAborted) or getattr(handle, "killed", False):
                        result = make_subagent_result(
                            spec,
                            session_id,
                            "interrupted",
                            summary=_partial_summary(spec, "Sub-agent was cancelled."),
                            error="CancelledError: Parent or runner cancelled sub-agent.",
                            exit_code=130,
                        )
                    else:
                        raise
                except (asyncio.TimeoutError, TimeoutError):
                    result = make_subagent_result(
                        spec,
                        session_id,
                        "timeout",
                        summary=f"Sub-agent timed out after {spec.timeout_seconds:.1f} seconds.",
                        error=f"TimeoutError: Sub-agent execution exceeded {spec.timeout_seconds}s limit.",
                        exit_code=124,
                    )
                last_result = result
                self._save_subagent_result(spec, result)
                if result.status in ("interrupted", "timeout", "failed"):
                    self.cancel_subagent(session_id)
                handle.result = result
                if handle.status != "interrupted":
                    handle.status = result.status
                handle.last_stop_reason = result.stop_reason
                get_agent_registry().changed()
                from coderai.wire.emitter import get_emitter
                from coderai.wire.types import SubagentEvent

                get_emitter().send(
                    SubagentEvent(
                        agent_id=handle.id,
                        subagent_type=spec.subagent_type,
                        event={
                            "type": "subagent.settled",
                            "status": handle.status,
                            "summary": handle.report or result.summary,
                        },
                    )
                )

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
                if getattr(handle, "killed", False):
                    return result
                waiter = getattr(handle, "inbox_waiter", None)
                if waiter is None:
                    return result
                idle_ttl = getattr(spec, "idle_ttl", None) or 300.0
                try:
                    await asyncio.wait_for(waiter.wait(), timeout=idle_ttl)
                except (asyncio.TimeoutError, TimeoutError):
                    handle.status = result.status
                    break
        finally:
            self._active_controllers.pop(session_id, None)
            _global_active_controllers.pop(session_id, None)
            clear_session_state(session_id)
            try:
                await asyncio.wait_for(
                    run_subagent_stop_async(
                        parent_sid,
                        self.project_root,
                        spec.subagent_type or spec.mode or "read_only",
                        (last_result.summary if last_result else "") or "",
                        settings=self.get_resolved_settings(),
                    ),
                    timeout=5.0,
                )
            except Exception:
                pass
        return last_result or make_subagent_result(
            spec,
            session_id,
            "interrupted",
            summary="Sub-agent was cancelled.",
            error="CancelledError: Parent or runner cancelled sub-agent.",
            exit_code=130,
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
        """Keep partial conversations protocol-valid when a turn is stopped."""
        from coderai.wire.emitter import get_emitter

        emitter = get_emitter()
        emitter.turn_begin(spec.prompt)
        from coderai.subagents.core import current_subagent_abort, current_subagent_checkpoint

        abort_token = current_subagent_abort.set(abort_event)
        checkpoint = state if state is not None else {}
        checkpoint_token = current_subagent_checkpoint.set(checkpoint)
        try:
            return await self._run_subagent_loop_inner(
                spec,
                session_id,
                abort_event,
                lifecycle_events,
                continuable=continuable,
                messages=messages,
                state=checkpoint,
            )
        finally:
            current_subagent_abort.reset(abort_token)
            current_subagent_checkpoint.reset(checkpoint_token)
            conversation = checkpoint.get("messages") or []
            if spec.handle is not None:
                spec.handle.conversation = conversation
            # A resumed conversation must answer every advertised tool call,
            # including calls not reached before SIGINT or a deadline.
            pending: dict[str, dict[str, Any]] = {}
            for message in conversation:
                if message.get("role") == "assistant":
                    for call in message.get("tool_calls") or []:
                        pending[str(call.get("id", ""))] = call
                elif message.get("role") == "tool":
                    pending.pop(str(message.get("tool_call_id", "")), None)
            for call_id in pending:
                conversation.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": json.dumps(
                            {
                                "ok": False,
                                "error": "Execution interrupted before a result was available.",
                            }
                        ),
                    }
                )
            partial = checkpoint.get("streaming_content") or ""
            thinking = checkpoint.get("streaming_thinking") or ""
            if (partial or thinking) and not checkpoint.get("streaming_recorded"):
                partial_message = {"role": "assistant", "content": partial}
                if thinking:
                    partial_message["reasoning_content"] = thinking
                conversation.append(partial_message)
            self._save_subagent_checkpoint(spec, conversation)
            emitter.turn_end()

    async def _run_subagent_loop_inner(
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
        from coderai.llm import ensure_oauth_fresh, prepare_oauth_request

        client_info = self.create_openai_client()
        if client_info.get("oauthKey"):
            await ensure_oauth_fresh(oauth=self.oauth_manager, oauth_keys=[client_info["oauthKey"]])
            client_info = self.create_openai_client()
        client = client_info.get("client")
        model = str(spec.model or client_info.get("model") or "gpt-6-luna")
        base_url = client_info.get("baseURL")
        temperature = client_info.get("temperature")
        thinking_enabled = bool(client_info.get("thinkingEnabled"))
        reasoning_effort = client_info.get("reasoningEffort") or "max"

        if client is None:
            return make_subagent_result(
                spec,
                session_id,
                "failed",
                summary="API key not found for sub-agent execution.",
                error="AuthenticationError: Missing API client.",
                exit_code=1,
                lifecycle_events=events,
            )

        # Setup sandboxed tools
        from coderai.tools.legacy.executor import ToolExecutor

        effective_root = spec.isolated_cwd or self.project_root
        mcp_manager = getattr(spec.session_manager, "mcp_manager", None)
        tool_executor = (
            ToolExecutor(effective_root, self.create_openai_client, mcp_manager=mcp_manager)
            if mcp_manager is not None
            else ToolExecutor(effective_root, self.create_openai_client)
        )
        available_tools = self._get_sandboxed_tools(spec, model)

        if messages is None:
            system_prompt = get_subagent_system_prompt(spec.mode or "read_only")
            if spec.seed_messages:
                system_prompt += (
                    "\n\nThe inherited conversation is a one-time snapshot from the parent, "
                    "provided as reference material. You are an independent subagent, "
                    "not a continuation of that agent. Complete your assigned task and report the result."
                )
            if spec.system_prompt:
                system_prompt = f"{system_prompt}\n\n## Role Instructions\n{spec.system_prompt}"
            runtime_context = get_runtime_context(effective_root, model)

            initial_user_prompt = spec.prompt
            if spec.description:
                initial_user_prompt = f"Goal: {spec.description}\n\n{initial_user_prompt}"
            if spec.extra_context:
                initial_user_prompt += f"\n\nAdditional Context:\n{spec.extra_context}"
            # Phase 2: explore agents orient with a <git-context> block
            # git repository context.
            if (spec.subagent_type or "").lower() == "explore":
                try:
                    from coderai.subagents.registry import build_explore_extra_context

                    git_block = await build_explore_extra_context(effective_root)
                    if git_block:
                        initial_user_prompt += f"\n\n{git_block}"
                except Exception:
                    pass
            if runtime_context:
                initial_user_prompt = f"{runtime_context}\n\n---\n\n{initial_user_prompt}"

            messages = [
                {"role": "system", "content": system_prompt},
            ]
            if spec.seed_messages:
                for sm in copy.deepcopy(spec.seed_messages):
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
            if state is not None:
                state["streaming_content"] = ""
                state["streaming_thinking"] = ""
                state["streaming_recorded"] = False
            from coderai.wire.emitter import get_emitter

            get_emitter().step_begin(iteration)
            if abort_event.is_set():
                return make_subagent_result(
                    spec,
                    session_id,
                    "interrupted",
                    summary=last_assistant_reply or "Sub-agent was interrupted.",
                    active_tokens=active_tokens,
                    total_prompt_tokens=total_prompt_tokens,
                    total_completion_tokens=total_completion_tokens,
                    total_cached_tokens=total_cached_tokens,
                    iteration=iteration,
                    tool_calls_count=tool_calls_count,
                    artifacts=artifacts,
                    diffs=diffs,
                    exit_code=130,
                    lifecycle_events=events,
                )

            # Check for queued steering / interactive messages from parent/user
            inbox_messages: list[str] = []
            if spec.handle and getattr(spec.handle, "inbox", None):
                while spec.handle.inbox:
                    inbox_messages.append(spec.handle.inbox.popleft())

            if inbox_messages:
                steering_text = "\n\n".join(
                    [f"[Steering from parent]: {msg}" for msg in inbox_messages]
                )
                messages.append({"role": "user", "content": steering_text})

            from coderai.llm import estimate_openai_request_tokens, apply_request_completion_cap
            from coderai.prompt import calculate_context_budget
            from coderai.soul.compaction import evaluate_compaction_trigger

            settings = self.get_resolved_settings()
            budget = calculate_context_budget(model, context_limit=settings.get("contextWindow"))
            estimated_context = estimate_openai_request_tokens(messages, available_tools)
            active_tokens = max(active_tokens, estimated_context)
            if evaluate_compaction_trigger(
                active_tokens,
                budget["context_limit"],
                pressure_ratio=float(settings.get("compactionTriggerRatio") or 0.85),
                reserved_context_size=int(settings.get("reservedContextSize", 50_000)),
            ):
                from coderai.subagents.compaction import compact_child_messages
                from coderai.subagents.core import current_subagent_checkpoint
                from coderai.wire.emitter import WireEmitter, bind_emitter, reset_emitter

                async def summarize(request: dict[str, Any]) -> dict[str, Any]:
                    # Summaries are scratchpad state, not child answer deltas.
                    quiet_emitter = WireEmitter()
                    token = bind_emitter(quiet_emitter)
                    checkpoint_token = current_subagent_checkpoint.set(None)
                    try:
                        apply_request_completion_cap(
                            request,
                            context_limit=budget["context_limit"],
                            response_budget=budget["max_output_tokens"],
                        )
                        return await asyncio.to_thread(_call_llm_sync, client, request)
                    finally:
                        current_subagent_checkpoint.reset(checkpoint_token)
                        reset_emitter(token)
                        quiet_emitter.close()

                get_emitter().compaction_begin()
                try:
                    usage = await compact_child_messages(
                        messages,
                        model=model,
                        complete=summarize,
                        summary_budget=budget["max_output_tokens"],
                    )
                    if usage is not None:
                        total_prompt_tokens += int(usage.get("prompt_tokens") or 0)
                        total_completion_tokens += int(usage.get("completion_tokens") or 0)
                        total_cached_tokens += int(usage.get("cached_tokens") or 0)
                        active_tokens = estimate_openai_request_tokens(messages, available_tools)
                        self._save_subagent_checkpoint(spec, messages)
                finally:
                    get_emitter().compaction_end()

            # Build request
            effective_messages = messages
            effective_tools: list[dict[str, Any]] | None = available_tools
            from coderai.utils.common.message_converter import (
                apply_cache_control_breakpoints,
                apply_tool_cache_control,
                is_cache_control_supported,
            )

            if is_cache_control_supported(model):
                effective_messages = apply_cache_control_breakpoints(messages, model)
                if effective_tools:
                    effective_tools = apply_tool_cache_control(effective_tools, model)

            request: dict[str, Any] = {
                "model": model,
                "messages": effective_messages,
                "tools": effective_tools if effective_tools else None,
                "stream": True,
                "stream_options": {"include_usage": True},
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
            completion_cap = apply_request_completion_cap(
                request,
                context_limit=budget["context_limit"],
                active_tokens=active_tokens,
                response_budget=budget["max_output_tokens"],
                reserved_context_size=int(settings.get("reservedContextSize", 50_000)),
            )
            if completion_cap < 1:
                return make_subagent_result(
                    spec,
                    session_id,
                    "budget_exceeded",
                    summary=last_assistant_reply or "Child context exhausted its model window.",
                    error="No completion budget remains after child context compaction.",
                    exit_code=2,
                    active_tokens=active_tokens,
                    total_prompt_tokens=total_prompt_tokens,
                    total_completion_tokens=total_completion_tokens,
                    total_cached_tokens=total_cached_tokens,
                    iteration=iteration,
                    tool_calls_count=tool_calls_count,
                    artifacts=artifacts,
                    diffs=diffs,
                    lifecycle_events=events,
                )

            # LLM invocation with retryable-failure backoff (harness retry
            # policy: 429/5xx/timeout/transport retried with jittered backoff)
            from coderai.utils.common.llm_retry import (
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
                    await prepare_oauth_request(client, oauth=self.oauth_manager)
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
                return make_subagent_result(
                    spec,
                    session_id,
                    "interrupted",
                    summary=last_assistant_reply or "Sub-agent was interrupted.",
                    active_tokens=active_tokens,
                    total_prompt_tokens=total_prompt_tokens,
                    total_completion_tokens=total_completion_tokens,
                    total_cached_tokens=total_cached_tokens,
                    iteration=iteration,
                    tool_calls_count=tool_calls_count,
                    artifacts=artifacts,
                    diffs=diffs,
                    exit_code=130,
                    lifecycle_events=events,
                )
            if response is None:
                return make_subagent_result(
                    spec,
                    session_id,
                    "failed",
                    summary=f"LLM request error in sub-agent: {last_error}",
                    error=str(last_error),
                    active_tokens=active_tokens,
                    total_prompt_tokens=total_prompt_tokens,
                    total_completion_tokens=total_completion_tokens,
                    total_cached_tokens=total_cached_tokens,
                    iteration=iteration,
                    tool_calls_count=tool_calls_count,
                    exit_code=1,
                    artifacts=artifacts,
                    diffs=diffs,
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
                return make_subagent_result(
                    spec,
                    session_id,
                    "budget_exceeded",
                    summary=last_assistant_reply
                    or f"Sub-agent reached token budget cap ({spec.token_budget} tokens).",
                    active_tokens=active_tokens,
                    total_prompt_tokens=total_prompt_tokens,
                    total_completion_tokens=total_completion_tokens,
                    total_cached_tokens=total_cached_tokens,
                    iteration=iteration,
                    tool_calls_count=tool_calls_count,
                    exit_code=2,
                    artifacts=artifacts,
                    diffs=diffs,
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

            if choice.get("finish_reason") == "length":
                return make_subagent_result(
                    spec,
                    session_id,
                    "budget_exceeded",
                    summary=last_assistant_reply,
                    error="Sub-agent response was truncated before completing its final summary.",
                    exit_code=2,
                    active_tokens=active_tokens,
                    total_prompt_tokens=total_prompt_tokens,
                    total_completion_tokens=total_completion_tokens,
                    total_cached_tokens=total_cached_tokens,
                    iteration=iteration,
                    tool_calls_count=tool_calls_count,
                    artifacts=artifacts,
                    diffs=diffs,
                    lifecycle_events=events,
                )

            if refusal:
                return make_subagent_result(
                    spec,
                    session_id,
                    "refusal",
                    summary=f"Model refused request: {refusal}",
                    error=refusal,
                    active_tokens=active_tokens,
                    total_prompt_tokens=total_prompt_tokens,
                    total_completion_tokens=total_completion_tokens,
                    total_cached_tokens=total_cached_tokens,
                    iteration=iteration,
                    tool_calls_count=tool_calls_count,
                    exit_code=1,
                    artifacts=artifacts,
                    diffs=diffs,
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
            self._save_subagent_checkpoint(spec, messages)
            if state is not None:
                state["streaming_recorded"] = True

            if not tool_calls:
                # Agent concluded with final response
                if not content.strip():
                    return make_subagent_result(
                        spec,
                        session_id,
                        "failed",
                        error="Sub-agent turn ended without a final message.",
                        exit_code=1,
                        active_tokens=active_tokens,
                        total_prompt_tokens=total_prompt_tokens,
                        total_completion_tokens=total_completion_tokens,
                        total_cached_tokens=total_cached_tokens,
                        iteration=iteration,
                        tool_calls_count=tool_calls_count,
                        artifacts=artifacts,
                        diffs=diffs,
                        lifecycle_events=events,
                    )
                return make_subagent_result(
                    spec,
                    session_id,
                    "completed",
                    summary=content or "Sub-agent completed task without text output.",
                    active_tokens=active_tokens,
                    total_prompt_tokens=total_prompt_tokens,
                    total_completion_tokens=total_completion_tokens,
                    total_cached_tokens=total_cached_tokens,
                    iteration=iteration,
                    tool_calls_count=tool_calls_count,
                    exit_code=0,
                    artifacts=artifacts,
                    diffs=diffs,
                    lifecycle_events=events,
                )

            # Execute tools in sandbox
            tool_calls_count += len(tool_calls)
            for tc in tool_calls:
                if abort_event.is_set():
                    break

                fn_name = tc.get("function", {}).get("name", "")
                fn_args_raw = tc.get("function", {}).get("arguments", "{}")
                from coderai.wire.types import ToolCallPart, ToolResultPart

                get_emitter().send(
                    ToolCallPart(
                        id=tc.get("id", ""),
                        name=fn_name,
                        arguments=fn_args_raw
                        if isinstance(fn_args_raw, str)
                        else json.dumps(fn_args_raw),
                    )
                )

                # Track examined artifacts/files
                parsed_args: Any = {}
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
                from coderai.tools.legacy.registry import get_tool_registry

                tdef = get_tool_registry().get_tool(fn_name)
                is_mutating = bool(tdef and tdef.is_mutating)

                # A child inherits the parent's scope policy as well as its
                # filesystem sandbox. Unapproved asks remain fail-closed.
                inherited_permissions = self.get_resolved_settings().get("permissions")
                permission_decision = "allow"
                if self._inherits_permission_settings or inherited_permissions:
                    from coderai.soul.approval import (
                        PLAN_MODE_FORCE_ASK_SCOPES,
                        compute_tool_call_permissions,
                    )

                    permission_plan = compute_tool_call_permissions(
                        session_id=session_id,
                        project_root=effective_root,
                        tool_calls=[tc],
                        settings=inherited_permissions,
                        force_ask_scopes=PLAN_MODE_FORCE_ASK_SCOPES if spec.plan_mode else None,
                    )
                    permission_decision = permission_plan["permissions"][0]["permission"]

                denial = subagent_tool_denial(
                    spec,
                    fn_name,
                    permission_decision,
                    is_mutating,
                    tdef.resolve_effects(parsed_args)
                    if tdef
                    else self._external_effects(spec, fn_name),
                )
                mcp_manager = getattr(spec.session_manager, "mcp_manager", None)
                if (
                    denial is None
                    and mcp_manager is not None
                    and mcp_manager.is_mcp_tool(fn_name)
                    and not mcp_manager.is_tool_enabled_for_session(spec.parent_session_id, fn_name)
                ):
                    denial = (
                        f"PermissionDenied: MCP tool '{fn_name}' is disabled in the parent session."
                    )
                if (
                    denial is None
                    and spec.mode == "read_only"
                    and fn_name in ("Task", "subagent", "subagent_fork")
                ):
                    denial = writable_child_denial(parsed_args, self.project_root)
                if denial is not None:
                    tool_result_content = json.dumps(
                        {"ok": False, "name": fn_name, "error": denial}
                    )
                else:
                    hooks = build_child_execution_hooks(
                        spec, abort_event, self._resolve_subagent_cwd(spec, session_id), diffs
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
                self._save_subagent_checkpoint(spec, messages)
                get_emitter().send(
                    ToolResultPart(
                        tool_call_id=tc.get("id", ""), output=tool_result_content or "(no output)"
                    )
                )

        # Exceeded max iterations
        return make_subagent_result(
            spec,
            session_id,
            "max_iterations",
            summary=last_assistant_reply
            or "Sub-agent reached max iteration limit before final conclusion.",
            active_tokens=active_tokens,
            total_prompt_tokens=total_prompt_tokens,
            total_completion_tokens=total_completion_tokens,
            total_cached_tokens=total_cached_tokens,
            iteration=spec.max_iterations,
            tool_calls_count=tool_calls_count,
            exit_code=2,
            artifacts=artifacts,
            diffs=diffs,
            lifecycle_events=events,
        )

    def _external_effects(self, spec: SubAgentSpec, name: str) -> str:
        from coderai.tools.legacy.policy import external_effects

        return external_effects(
            name,
            SimpleNamespace(
                session_manager=spec.session_manager,
                project_root=spec.project_root or self.project_root,
            ),
        )

    def _get_sandboxed_tools(self, spec: SubAgentSpec, model: str) -> list[dict[str, Any]]:
        """Filter tools for subagent execution with deterministic ordering and canonicalization."""
        import copy
        from coderai.prompt import format_tool_definitions
        from coderai.prompt.sections import order_tools
        from coderai.tools.legacy.types import canonicalize_tool_schema
        from coderai.tools.legacy.registry import get_tool_registry

        external_tools = None
        if spec.session_manager is not None:
            external_definitions = getattr(
                spec.session_manager, "get_external_tool_definitions", None
            )
            if callable(external_definitions):
                external_tools = external_definitions()
            mcp_manager = getattr(spec.session_manager, "mcp_manager", None)
            if mcp_manager is not None:
                external_tools = [
                    tool
                    for tool in external_tools or []
                    if not mcp_manager.is_mcp_tool(tool.get("function", {}).get("name", ""))
                    or mcp_manager.is_tool_enabled_for_session(
                        spec.parent_session_id, tool.get("function", {}).get("name", "")
                    )
                ]
        all_tools = get_tools(
            {"model": model, "nonInteractive": True, "childAgent": True},
            external_tools=external_tools,
        )
        reg = get_tool_registry()

        filtered: list[dict[str, Any]] = []
        for tool in all_tools:
            name = tool.get("function", {}).get("name", "")

            tdef = reg.get_tool(name)
            effects = tdef.resolve_effects({}) if tdef else self._external_effects(spec, name)
            if subagent_tool_denial(spec, name, "allow", bool(tdef and tdef.is_mutating), effects):
                continue
            if spec.mode == "read_only" and name in {"Task", "subagent", "subagent_fork"}:
                tool = copy.deepcopy(tool)
                params = tool.get("function", {}).get("parameters", {}).get("properties", {})
                if "mode" in params:
                    params["mode"].update(enum=["read_only"], default="read_only")

            filtered.append(tool)

        formatted = format_tool_definitions(filtered, model=model)
        return order_tools([canonicalize_tool_schema(t) for t in formatted])
