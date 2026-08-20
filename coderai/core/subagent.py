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
from coderai.core.prompt import get_runtime_context, get_subagent_system_prompt, get_tools
from coderai.core.state import clear_session_state
from coderai.core.tools.types import ToolExecutionHooks

logger = logging.getLogger(__name__)

MAX_SUBAGENT_ITERATIONS = 20
MAX_SUBAGENT_DEPTH = 3
DEFAULT_SUBAGENT_TIMEOUT = 90.0


@dataclass
class SubAgentSpec:
    """Specification for spawning an isolated sub-agent."""

    description: str
    prompt: str
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    mode: str = "read_only"  # "read_only" | "general"
    timeout_seconds: float = DEFAULT_SUBAGENT_TIMEOUT
    max_iterations: int = MAX_SUBAGENT_ITERATIONS
    depth: int = 0
    parent_session_id: str | None = None
    allowed_tools: list[str] | None = None
    extra_context: str | None = None


@dataclass
class SubAgentResult:
    """Aggregated output from a sub-agent execution."""

    task_id: str
    session_id: str
    status: str  # "completed" | "failed" | "interrupted" | "timeout" | "max_iterations"
    summary: str
    active_tokens: int = 0
    total_tokens: int = 0
    iterations: int = 0
    tool_calls_count: int = 0
    error: str | None = None
    artifacts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "status": self.status,
            "summary": self.summary,
            "active_tokens": self.active_tokens,
            "total_tokens": self.total_tokens,
            "iterations": self.iterations,
            "tool_calls_count": self.tool_calls_count,
            "error": self.error,
            "artifacts": self.artifacts,
        }

    def format_markdown(self) -> str:
        status_badge = "✅ COMPLETED" if self.status == "completed" else f"⚠️ {self.status.upper()}"
        lines = [
            f"### Sub-Agent Task Result [{self.task_id}] — {status_badge}",
            f"**Status**: `{self.status}` | **Iterations**: `{self.iterations}` | **Tokens**: `{self.total_tokens}`",
        ]
        if self.error:
            lines.append(f"\n> ❌ **Error**: {self.error}\n")
        lines.append("\n**Findings & Summary**:")
        lines.append(self.summary.strip() or "No summary provided.")
        if self.artifacts:
            lines.append("\n**Artifacts/Files Examined**:")
            for art in self.artifacts:
                lines.append(f"- `{art}`")
        return "\n".join(lines)


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

    def cancel_subagent(self, session_id: str) -> None:
        """Cancel a running sub-agent session."""
        event = self._active_controllers.get(session_id)
        if event is not None:
            event.set()

    def cancel_all(self) -> None:
        """Cancel all running sub-agents."""
        for event in list(self._active_controllers.values()):
            event.set()

    async def spawn_subagent(self, spec: SubAgentSpec) -> SubAgentResult:
        """Spawn and execute a single isolated sub-agent with timeout and error recovery."""
        session_id = (
            f"sub_{spec.parent_session_id[:8] if spec.parent_session_id else 'root'}_{spec.task_id}"
        )
        if spec.depth > MAX_SUBAGENT_DEPTH:
            return SubAgentResult(
                task_id=spec.task_id,
                session_id=session_id,
                status="failed",
                summary="Maximum sub-agent nesting depth exceeded.",
                error="RecursionLimitError: Sub-agents cannot spawn additional sub-agents.",
            )

        abort_event = asyncio.Event()
        self._active_controllers[session_id] = abort_event

        try:
            return await asyncio.wait_for(
                self._run_subagent_loop(spec, session_id, abort_event),
                timeout=spec.timeout_seconds,
            )
        except asyncio.TimeoutError:
            return SubAgentResult(
                task_id=spec.task_id,
                session_id=session_id,
                status="timeout",
                summary=f"Sub-agent timed out after {spec.timeout_seconds:.1f} seconds.",
                error=f"TimeoutError: Sub-agent execution exceeded {spec.timeout_seconds}s limit.",
            )
        except asyncio.CancelledError:
            return SubAgentResult(
                task_id=spec.task_id,
                session_id=session_id,
                status="interrupted",
                summary="Sub-agent was cancelled.",
                error="CancelledError: Parent or runner cancelled sub-agent.",
            )
        except Exception as e:
            logger.exception("Sub-agent execution error")
            return SubAgentResult(
                task_id=spec.task_id,
                session_id=session_id,
                status="failed",
                summary=f"Sub-agent encountered an error: {e}",
                error=str(e),
            )
        finally:
            self._active_controllers.pop(session_id, None)
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

    async def _run_subagent_loop(
        self,
        spec: SubAgentSpec,
        session_id: str,
        abort_event: asyncio.Event,
    ) -> SubAgentResult:
        """Run the isolated agentic loop for the sub-agent."""
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
            )

        # Setup sandboxed tools
        from coderai.core.tools.executor import ToolExecutor

        tool_executor = ToolExecutor(self.project_root, self.create_openai_client)
        available_tools = self._get_sandboxed_tools(spec, model)

        # Build isolated initial message history
        system_prompt = get_subagent_system_prompt(spec.mode, spec.description)
        runtime_context = get_runtime_context(self.project_root, model)

        initial_user_prompt = spec.prompt
        if spec.extra_context:
            initial_user_prompt += f"\n\nAdditional Context:\n{spec.extra_context}"

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "system", "content": runtime_context},
            {"role": "user", "content": initial_user_prompt},
        ]

        total_prompt_tokens = 0
        total_completion_tokens = 0
        active_tokens = 0
        tool_calls_count = 0
        last_assistant_reply = ""
        artifacts: list[str] = []

        for iteration in range(1, spec.max_iterations + 1):
            if abort_event.is_set():
                return SubAgentResult(
                    task_id=spec.task_id,
                    session_id=session_id,
                    status="interrupted",
                    summary=last_assistant_reply or "Sub-agent was interrupted.",
                    active_tokens=active_tokens,
                    total_tokens=total_prompt_tokens + total_completion_tokens,
                    iterations=iteration,
                    tool_calls_count=tool_calls_count,
                    artifacts=artifacts,
                )

            # Build request
            request: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "tools": available_tools if available_tools else None,
            }
            if temperature is not None:
                request["temperature"] = temperature
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

            # LLM invocation
            try:
                response = await asyncio.to_thread(_call_llm_sync, client, request)
            except (asyncio.CancelledError, TimeoutError):
                raise
            except Exception as e:
                return SubAgentResult(
                    task_id=spec.task_id,
                    session_id=session_id,
                    status="failed",
                    summary=f"LLM request error in sub-agent: {e}",
                    active_tokens=active_tokens,
                    total_tokens=total_prompt_tokens + total_completion_tokens,
                    iterations=iteration,
                    tool_calls_count=tool_calls_count,
                    error=str(e),
                    artifacts=artifacts,
                )

            usage = response.get("usage") or {}
            p_tok = usage.get("prompt_tokens", 0)
            c_tok = usage.get("completion_tokens", 0)
            total_prompt_tokens += p_tok
            total_completion_tokens += c_tok
            active_tokens = usage.get("total_tokens", p_tok + c_tok)

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
                    status="failed",
                    summary=f"Model refused request: {refusal}",
                    active_tokens=active_tokens,
                    total_tokens=total_prompt_tokens + total_completion_tokens,
                    iterations=iteration,
                    tool_calls_count=tool_calls_count,
                    error=refusal,
                    artifacts=artifacts,
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
                    iterations=iteration,
                    tool_calls_count=tool_calls_count,
                    artifacts=artifacts,
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
                        "content": tool_result_content,
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
            iterations=spec.max_iterations,
            tool_calls_count=tool_calls_count,
            artifacts=artifacts,
        )

    def _get_sandboxed_tools(self, spec: SubAgentSpec, model: str) -> list[dict[str, Any]]:
        """Filter tools for subagent execution."""
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

            filtered.append(tool)

        return filtered


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
    """Synchronous LLM call wrapper."""
    resp = client.chat.completions.create(**request)
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
    usage = getattr(resp, "usage", None)
    if usage:
        res["usage"] = {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0),
            "completion_tokens": getattr(usage, "completion_tokens", 0),
            "total_tokens": getattr(usage, "total_tokens", 0),
        }
    return res
