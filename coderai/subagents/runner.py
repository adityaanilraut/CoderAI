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
from typing import Any
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
    cleanup_subagent_scratchpad,
    setup_subagent_scratchpad,
)
from coderai.file_snippets import clear_session_state
from coderai.tools.legacy.types import ToolExecutionHooks

logger = logging.getLogger(__name__)
from coderai.subagents.builder import (
    MAX_SUBAGENT_DEPTH,
    SubAgentSpec,
    build_spec as build_spec,
)
from coderai.subagents.core import _call_llm_sync, _normalize_subagent_tool_calls
from coderai.subagents.output import SubAgentResult

_global_active_controllers: dict[str, asyncio.Event] = {}


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
        token_telemetry=token_telemetry,
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
        if create_openai_client is None:
            from coderai.llm import create_openai_client as _default_create_client

            self.create_openai_client = lambda: _default_create_client(self.project_root)
        else:
            self.create_openai_client = create_openai_client
        self.get_resolved_settings = get_resolved_settings or (lambda: {})
        self.message_converter = OpenAIMessageConverter()
        self._active_controllers: dict[str, asyncio.Event] = {}

    def _resolve_subagent_cwd(self, spec: SubAgentSpec, session_id: str) -> str | None:
        if spec.isolated_cwd:
            return spec.isolated_cwd
        if getattr(spec, "isolate", False) and spec.scratchpad_dir:
            return spec.scratchpad_dir
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
        from coderai.subagents.core import AgentHandle, get_agent_registry

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
        )
        get_agent_registry().register(handle)

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
        handle.result = result
        handle.status = result.status
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

        has_explicit_isolation = bool(spec.isolated_cwd or getattr(spec, "isolate", False))
        if has_explicit_isolation and not spec.scratchpad_dir:
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

        from coderai.hooks.runner import run_on_subagent_spawn, run_subagent_start

        parent_sid = spec.parent_session_id or "root"
        try:
            run_on_subagent_spawn(
                parent_session_id=parent_sid,
                subagent_id=session_id,
                task=spec.prompt or spec.description,
                mode=spec.mode,
                project_root=self.project_root,
            )
        except Exception:
            pass
        # SubagentStart fires alongside legacy SubagentSpawn.
        try:
            run_subagent_start(
                parent_sid,
                self.project_root,
                getattr(spec, "agent_type", None) or spec.mode,
                spec.prompt or spec.description,
            )
        except Exception:
            pass

        outcome_summary: str = ""

        try:
            if spec.provider == "claude_code":
                from coderai.subagents.backends.claude_code import (
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
                        cwd=self.project_root,
                    )
                )
                raw_res = await asyncio.wait_for(
                    codex_driver.execute(spec.prompt, project_root=self.project_root),
                    timeout=spec.timeout_seconds,
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
                        cwd=self.project_root,
                        timeout_seconds=spec.timeout_seconds,
                    )
                )
                raw_res = await asyncio.wait_for(
                    runner.execute(spec.prompt),
                    timeout=spec.timeout_seconds,
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
        except asyncio.CancelledError:
            self._emit_lifecycle_event(
                lifecycle_events,
                "subagent/error",
                spec,
                {"error": "CancelledError: Parent or runner cancelled sub-agent."},
            )
            outcome_summary = "Sub-agent was cancelled."
            if abort_event.is_set():
                return make_subagent_result(
                    spec,
                    session_id,
                    "interrupted",
                    summary="Sub-agent was cancelled.",
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
            if has_explicit_isolation and spec.scratchpad_dir:
                try:
                    cleanup_subagent_scratchpad(spec.scratchpad_dir)
                except Exception:
                    pass
            try:
                from coderai.hooks.runner import run_subagent_stop

                run_subagent_stop(
                    parent_sid,
                    self.project_root,
                    getattr(spec, "agent_type", None) or spec.mode,
                    outcome_summary,
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
        from coderai.orchestration import settlement_summary

        handle = spec.handle
        if handle is None:
            return await self._spawn_subagent_inner(spec, session_id)

        state: dict[str, Any] = {}
        abort_event = asyncio.Event()
        self._active_controllers[session_id] = abort_event
        last_result: SubAgentResult | None = None
        from coderai.hooks.runner import (
            run_on_subagent_spawn,
            run_subagent_start,
            run_subagent_stop,
        )

        parent_sid = spec.parent_session_id or "root"
        try:
            run_on_subagent_spawn(
                parent_session_id=parent_sid,
                subagent_id=session_id,
                task=spec.prompt or spec.description,
                mode=spec.mode,
                project_root=self.project_root,
            )
        except Exception:
            pass
        try:
            run_subagent_start(
                parent_sid,
                self.project_root,
                getattr(spec, "agent_type", None) or spec.mode,
                spec.prompt or spec.description,
            )
        except Exception:
            pass
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
                    if abort_event.is_set() or getattr(handle, "killed", False):
                        result = make_subagent_result(
                            spec,
                            session_id,
                            "interrupted",
                            summary="Sub-agent was cancelled.",
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
                if getattr(handle, "killed", False):
                    return result
                waiter = getattr(handle, "inbox_waiter", None)
                if waiter is None:
                    return result
                idle_ttl = getattr(spec, "idle_ttl", None) or 300.0
                try:
                    await asyncio.wait_for(waiter.wait(), timeout=idle_ttl)
                except (asyncio.TimeoutError, TimeoutError):
                    handle.status = "completed"
                    break
        finally:
            self._active_controllers.pop(session_id, None)
            clear_session_state(session_id)
            try:
                run_subagent_stop(
                    parent_sid,
                    self.project_root,
                    getattr(spec, "agent_type", None) or spec.mode,
                    (last_result.summary if last_result else "") or "",
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
        """Run the isolated agentic loop for the sub-agent.

        ``continuable=True`` keeps the run alive across turns: the supplied
        ``messages`` conversation is reused, the terminal result is parked
        (the caller owns re-entry), and the conversation is stashed on
        ``state["messages"]`` for the next turn.
        """
        events = lifecycle_events if lifecycle_events is not None else []
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
        tool_executor = ToolExecutor(effective_root, self.create_openai_client)
        available_tools = self._get_sandboxed_tools(spec, model)

        if messages is None:
            system_prompt = get_subagent_system_prompt(spec.mode)
            if getattr(spec, "system_prompt", None):
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
                    inbox_messages.append(spec.handle.inbox.pop(0))
            elif spec.agent_id or spec.task_id:
                from coderai.subagents.core import get_agent_registry

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
            effective_messages = messages
            effective_tools = available_tools
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

            if not tool_calls:
                # Agent concluded with final response
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
                from coderai.subagents.registry import is_tool_allowed, get_subagent_definition
                from coderai.tools.legacy.registry import get_tool_registry

                tdef = get_tool_registry().get_tool(fn_name)
                is_mutating = bool(tdef and tdef.is_mutating)

                if spec.exclude_tools and is_tool_allowed(
                    fn_name, "allowlist", tuple(spec.exclude_tools)
                ):
                    tool_result_content = json.dumps(
                        {
                            "ok": False,
                            "name": fn_name,
                            "error": f"PermissionDenied: Tool '{fn_name}' is excluded by sub-agent policy.",
                        }
                    )
                elif spec.allowed_tools is not None and not is_tool_allowed(
                    fn_name, "allowlist", tuple(spec.allowed_tools)
                ):
                    tool_result_content = json.dumps(
                        {
                            "ok": False,
                            "name": fn_name,
                            "error": f"PermissionDenied: Tool '{fn_name}' is not permitted by sub-agent allowlist policy.",
                        }
                    )
                elif spec.mode == "read_only" and (
                    fn_name in ("bash", "pwsh") and spec.sandbox_mode != "read-only"
                ):
                    tool_result_content = json.dumps(
                        {
                            "ok": False,
                            "name": fn_name,
                            "error": f"PermissionDenied: Sub-agent in read_only mode cannot invoke shell tool '{fn_name}' outside read-only sandbox.",
                        }
                    )
                elif spec.mode == "read_only" and (
                    fn_name
                    in (
                        "write",
                        "Write",
                        "edit",
                        "Edit",
                        "str_replace_editor",
                        "schedule_create",
                        "schedule_delete",
                        "spawn_teammate",
                    )
                    or fn_name.startswith("terminal_")
                    or (is_mutating and fn_name not in ("bash", "pwsh"))
                ):
                    tool_result_content = json.dumps(
                        {
                            "ok": False,
                            "name": fn_name,
                            "error": f"PermissionDenied: Sub-agent in read_only mode cannot invoke mutating tool '{fn_name}'.",
                        }
                    )
                elif spec.mode == "read_only" and fn_name in ("Task", "subagent", "subagent_fork"):
                    child_mode = parsed_args.get("mode") if isinstance(parsed_args, dict) else None
                    child_type = (
                        parsed_args.get("subagent_type") if isinstance(parsed_args, dict) else None
                    )
                    child_is_writable = False
                    if child_mode == "general":
                        child_is_writable = True
                    elif not child_mode and child_type:
                        c_def = get_subagent_definition(child_type)
                        if c_def and c_def.mode != "read_only":
                            child_is_writable = True
                    elif not child_mode and not child_type:
                        child_is_writable = True

                    if child_is_writable:
                        tool_result_content = json.dumps(
                            {
                                "ok": False,
                                "name": fn_name,
                                "error": "PermissionDenied: Sub-agent in read_only mode cannot spawn a writable child sub-agent.",
                            }
                        )
                    else:
                        hooks = ToolExecutionHooks(
                            should_stop=lambda: abort_event.is_set(),
                            isolated_cwd=self._resolve_subagent_cwd(spec, session_id),
                            dry_run=spec.dry_run,
                            on_before_file_mutation=spec.on_before_file_mutation,
                            on_after_file_mutation=lambda fp: (
                                diffs.append({"file_path": str(fp)}),
                                spec.on_after_file_mutation(fp)
                                if spec.on_after_file_mutation
                                else None,
                            ),
                            sandbox_mode=spec.sandbox_mode,
                            plan_mode=spec.plan_mode,
                            session_manager=spec.session_manager,
                            allowed_tools=spec.allowed_tools,
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
                else:
                    hooks = ToolExecutionHooks(
                        should_stop=lambda: abort_event.is_set(),
                        isolated_cwd=self._resolve_subagent_cwd(spec, session_id),
                        dry_run=spec.dry_run,
                        on_before_file_mutation=spec.on_before_file_mutation,
                        on_after_file_mutation=lambda fp: (
                            diffs.append({"file_path": str(fp)}),
                            spec.on_after_file_mutation(fp)
                            if spec.on_after_file_mutation
                            else None,
                        ),
                        sandbox_mode=spec.sandbox_mode,
                        plan_mode=spec.plan_mode,
                        session_manager=spec.session_manager,
                        allowed_tools=spec.allowed_tools,
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

    def _get_sandboxed_tools(self, spec: SubAgentSpec, model: str) -> list[dict[str, Any]]:
        """Filter tools for subagent execution with deterministic ordering and canonicalization."""
        import copy
        from coderai.prompt import format_tool_definitions
        from coderai.prompt.sections import order_tools
        from coderai.tools.legacy.types import canonicalize_tool_schema
        from coderai.subagents.registry import is_tool_allowed
        from coderai.tools.legacy.registry import get_tool_registry

        all_tools = get_tools({"model": model, "nonInteractive": True, "childAgent": True})
        reg = get_tool_registry()

        filtered: list[dict[str, Any]] = []
        for tool in all_tools:
            name = tool.get("function", {}).get("name", "")

            # Sub-agents never ask interactive user questions
            if name == "AskUserQuestion":
                continue

            # Limit sub-agent recursion
            if name in ("Task", "subagent", "subagent_fork") and spec.depth >= MAX_SUBAGENT_DEPTH:
                continue

            # Exclude tools check
            if spec.exclude_tools and is_tool_allowed(name, "allowlist", tuple(spec.exclude_tools)):
                continue

            # Allowlist check (None = inherit, [] = deny all)
            if spec.allowed_tools is not None:
                if not is_tool_allowed(name, "allowlist", tuple(spec.allowed_tools)):
                    continue

            # Read-only mode disallows mutating tools and shell unless sandbox is read-only
            if spec.mode == "read_only":
                tdef = reg.get_tool(name)
                is_mutating = bool(tdef and tdef.is_mutating)

                if name in ("bash", "pwsh"):
                    if spec.sandbox_mode != "read-only":
                        continue
                elif (
                    name
                    in (
                        "write",
                        "Write",
                        "edit",
                        "Edit",
                        "str_replace_editor",
                        "schedule_create",
                        "schedule_delete",
                        "spawn_teammate",
                    )
                    or name.startswith("terminal_")
                    or (is_mutating and name not in ("bash", "pwsh"))
                ):
                    continue
                elif name in ("Task", "subagent", "subagent_fork"):
                    # Restrict tool schema so it only advertises read_only mode
                    tool = copy.deepcopy(tool)
                    params = tool.get("function", {}).get("parameters", {}).get("properties", {})
                    if "mode" in params:
                        params["mode"]["enum"] = ["read_only"]
                        params["mode"]["default"] = "read_only"

            if (
                spec.descriptor
                and spec.descriptor.tool_filter
                and not spec.descriptor.tool_filter.is_tool_permitted(name)
            ):
                continue

            filtered.append(tool)

        formatted = format_tool_definitions(filtered, model=model)
        return order_tools([canonicalize_tool_schema(t) for t in formatted])
