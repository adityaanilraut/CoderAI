"""LLM request, streaming, usage, and retry phase."""

import asyncio
import logging
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any, Optional, cast

from coderAI.core.agent_tracker import AgentStatus
from coderAI.core.agent_loop_outcomes import CANCELLED_REQUEST
from coderAI.core.ports import AgentRuntime, RuntimeView
from coderAI.core.services import get_services
from coderAI.core.turn import TurnContext
from coderAI.llm.base import normalize_usage
from coderAI.system.error_policy import (
    BudgetExceededError,
    MAX_RETRIES_PER_ITERATION,
    check_budget_limit,
    compute_retry_delay,
    is_transient_error,
)

logger = logging.getLogger(__name__)


class LLMPhase:
    """Typed mixin contract for the LLM side of ``ExecutionLoop``."""

    agent: AgentRuntime
    runtime: RuntimeView
    _length_retry_used: bool

    if TYPE_CHECKING:

        def _inject_step_reminders(
            self,
            messages: list[dict[str, Any]],
            iteration: int,
            max_iterations: int,
        ) -> list[dict[str, Any]]: ...

        def _get_tool_schemas(
            self,
            objective: str = "",
            *,
            warm_tool_names: Optional[set[str]] = None,
        ) -> Optional[list[dict[str, Any]]]: ...

    async def _handle_llm_phase(self, state: TurnContext) -> dict[str, Any]:
        """Call the LLM (including the one-shot ``length`` retry) and return
        the parsed response data."""
        info = self.agent.tracker_info
        if info and info.status != AgentStatus.THINKING:
            self.agent.tracker_update(status=AgentStatus.THINKING)

        # Inject step reminders (plan mode, step-limit warnings)
        step_aware_messages = self._inject_step_reminders(
            state.messages, state.iteration, state.max_iterations
        )

        # Periodic MCP server health check (every 10 iterations across the
        # Agent lifetime — not per turn). Launched in the background so the
        # SSE probes (5s timeout each) and reconnect back-off sleeps never
        # stall the agent's reasoning loop.
        counter = self.runtime.mcp_health_check_counter
        if not isinstance(counter, int):
            counter = 0
        counter += 1
        if counter >= 10:
            counter = 0
            self._maybe_start_mcp_health_check()
        self.agent._mcp_health_check_counter = counter

        # A completed background health check / reconnect, list_changed
        # notification, or mcp_connect/disconnect may have changed servers;
        # rebuild schemas on this thread before the next LLM call. Dirty
        # flags live on Agent + MCPClient so they survive turn boundaries.
        # High-impact: incremental diff — only rebuild when changed server
        # set is non-empty, and track last known mcp schema names to avoid
        # redundant full routing on spurious dirty flags.
        schemas_dirty = self.runtime.tool_schemas_dirty
        client_dirty = False
        changed_servers: set[str] = set()
        try:
            mcp_client = get_services().mcp_client
            client_dirty = mcp_client._schemas_dirty is True
            if client_dirty:
                mcp_client._schemas_dirty = False
                # Track which servers actually changed (degraded vs healthy)
                try:
                    degraded = {
                        name for name, info in mcp_client.servers.items() if info.get("degraded")
                    }
                    # Use degraded set as proxy for changed; full diff would
                    # require mcp_client._last_discovered cache
                    changed_servers = degraded
                except Exception:
                    pass
        except Exception:
            pass
        # Avoid redundant routing when dirty was set but no server topology changed
        should_rebuild = schemas_dirty or client_dirty
        if should_rebuild:
            # If client_dirty but degraded set empty and no new servers, still
            # rebuild once to be safe; otherwise incremental rebuild is fine.
            # We keep the single _get_tool_schemas call but skip when the
            # previous schemas already contain the same mcp names.
            last_mcp_names = self.agent._last_mcp_schema_names
            try:
                mcp_client = get_services().mcp_client
                current_mcp_names = frozenset(
                    s.get("function", {}).get("name", "")
                    for s in mcp_client.get_tools_as_openai_format()
                )
            except Exception:
                current_mcp_names = last_mcp_names
            if not schemas_dirty and current_mcp_names == last_mcp_names and not changed_servers:
                # Spurious dirty — skip rebuild
                self.agent._tool_schemas_dirty = False
            else:
                self.agent._tool_schemas_dirty = False
                self.agent._last_mcp_schema_names = current_mcp_names
                state.tool_schemas = self._get_tool_schemas(
                    state.user_message,
                    warm_tool_names=state.warm_tool_names,
                )
                state.routed_tool_names = {
                    str((schema.get("function") or {}).get("name"))
                    for schema in state.tool_schemas or []
                    if (schema.get("function") or {}).get("name")
                }

        response_data = await self._call_llm_with_retry(step_aware_messages, state.tool_schemas)

        # One-shot recovery when the model gets cut off mid-tool-loop:
        # ask once for a concise final answer and re-issue the call.
        # Second consecutive ``length`` is terminal (handled by the
        # finish-reason phase).
        if (
            response_data.get("finish_reason") == "length"
            and state.tools_were_used
            and not self._length_retry_used
        ):
            self._length_retry_used = True
            get_services().events.emit(
                "agent_warning",
                message=(
                    "Response truncated mid-tool-loop; retrying once with "
                    "a concise-final-answer hint."
                ),
            )
            step_aware_messages = list(step_aware_messages) + [
                {
                    "role": "system",
                    "content": (
                        "Previous reply was truncated by max_tokens. "
                        "Respond concisely with the final answer for the "
                        "user. Do NOT call any more tools."
                    ),
                }
            ]
            response_data = await self._call_llm_with_retry(step_aware_messages, state.tool_schemas)

        return response_data

    def _maybe_start_mcp_health_check(self) -> None:
        """Run the MCP health check + auto-reconnect off the critical path.

        ``check_server_health`` makes per-server network probes (5s timeout
        each) and ``auto_reconnect_degraded`` sleeps through an exponential
        back-off — both would otherwise stall the agent's LLM loop for
        seconds. We run them as a detached task on the ``Agent`` (so the flag
        survives the per-message ``ExecutionLoop``) and only signal a schema
        rebuild once the work finishes. At most one health task runs at a time.
        """
        task = self.runtime.mcp_health_task
        if task is not None and not task.done():
            return

        agent = self.agent

        async def _run_health_check() -> None:
            try:
                mcp_client = get_services().mcp_client

                await mcp_client.check_server_health()
                await mcp_client.auto_reconnect_degraded()
                # Defer the schema rebuild to the loop thread (it owns
                # ``state.tool_schemas``); flag on the Agent so the next turn
                # still sees it if this turn already ended.
                agent._tool_schemas_dirty = True
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("MCP health check failed: %s", e)

        old = agent._mcp_health_task
        if old is not None and not old.done():
            old.cancel()
        agent._mcp_health_task = asyncio.create_task(_run_health_check())

    async def _call_llm_with_retry(
        self,
        messages: list[dict[str, Any]],
        tool_schemas: Optional[list[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Call the LLM with retry logic for transient errors."""
        controller = self.agent.context_controller
        controller.request_tool_schemas = tool_schemas
        messages = await controller.manage_context_window(messages)
        session = self.agent.session
        has_summary = any(message.get(controller._SUMMARY_MARKER_KEY) for message in messages)
        if session is not None:
            if has_summary:
                session.set_context_messages(messages)
            elif session.context_messages is not None:
                session.clear_context_messages()
        provider_messages = self.agent.context_controller.strip_internal_markers(messages)
        provider_messages = self.agent.provider.clean_messages(provider_messages)
        for attempt in range(1, MAX_RETRIES_PER_ITERATION + 1):
            try:
                if self.agent.streaming:
                    raw_result = await self._await_llm_request(
                        self._stream_response(provider_messages, tool_schemas)
                    )
                    if raw_result is CANCELLED_REQUEST:
                        return {"content": None, "tool_calls": None, "finish_reason": "cancelled"}
                    result = cast(dict[str, Any], raw_result)
                else:
                    raw = await self._await_llm_request(
                        self.agent.provider.chat(provider_messages, tools=tool_schemas)
                    )
                    if raw is CANCELLED_REQUEST:
                        return {"content": None, "tool_calls": None, "finish_reason": "cancelled"}
                    result = self._extract_response_data(raw)

                # Attribute this call's usage/cost from the response's per-call
                # ``usage`` (canonical schema) — no diffing of provider-side
                # cumulative counters, so a mid-session model/provider swap needs
                # no re-sync and the totals stay continuous.
                usage = normalize_usage(result.get("usage"))
                new_in = usage["input_tokens"]
                new_out = usage["output_tokens"]
                self.agent.total_prompt_tokens += new_in
                self.agent.total_completion_tokens += new_out
                self.agent.total_tokens += new_in + new_out
                self.agent.total_cache_creation_tokens += usage["cache_creation_tokens"]
                self.agent.total_cache_read_tokens += usage["cache_read_tokens"]

                if new_in > 0 or new_out > 0:
                    model_for_cost = self.agent.provider.actual_model
                    cost_delta = await self.agent.cost_tracker.add_cost(
                        model_for_cost, new_in, new_out
                    )
                    try:
                        get_services().events.emit(
                            "cost_delta",
                            model=model_for_cost,
                            input_tokens=new_in,
                            output_tokens=new_out,
                            cost_delta=cost_delta,
                            total_cost=self.agent.cost_tracker.get_total_cost(),
                        )
                    except Exception:
                        pass
                    check_budget_limit(
                        self.agent.config.budget_limit,
                        self.agent.cost_tracker,
                        emit_warning=True,
                    )

                return result
            except BudgetExceededError:
                # Never retry a budget failure — it's a hard stop, not a blip.
                raise
            except Exception as e:
                if not is_transient_error(e) or attempt == MAX_RETRIES_PER_ITERATION:
                    raise
                delay = compute_retry_delay(e, attempt)
                # Add jitter to avoid thundering herd on rate limits
                import random as _r2

                delay = delay * (0.8 + _r2.random() * 0.4)
                logger.warning(
                    f"Transient error (attempt {attempt}/{MAX_RETRIES_PER_ITERATION}): "
                    f"{e}. Retrying in {delay:.1f}s…"
                )
                get_services().events.emit(
                    "agent_warning",
                    message=f"Transient error, retrying in {delay:.1f}s… ({attempt}/{MAX_RETRIES_PER_ITERATION})",
                )
                tracker_info = self.agent.tracker_info
                cancel_event = tracker_info._cancel_event if tracker_info else None
                if cancel_event is None:
                    await asyncio.sleep(delay)
                else:
                    try:
                        await asyncio.wait_for(cancel_event.wait(), timeout=delay)
                        return {"content": None, "tool_calls": None, "finish_reason": "cancelled"}
                    except asyncio.TimeoutError:
                        pass

        raise RuntimeError("_call_llm_with_retry exhausted without returning or raising")

    async def _await_llm_request(self, awaitable: Awaitable[Any]) -> Any:
        """Race one provider request against the active turn's cancellation event."""
        tracker_info = self.agent.tracker_info
        cancel_event = tracker_info._cancel_event if tracker_info else None
        if cancel_event is None:
            return await awaitable

        request_task = asyncio.ensure_future(awaitable)
        cancel_task = asyncio.create_task(cancel_event.wait())
        done, _ = await asyncio.wait(
            {request_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if cancel_task in done:
            request_task.cancel()
            await asyncio.gather(request_task, return_exceptions=True)
            return CANCELLED_REQUEST
        cancel_task.cancel()
        await asyncio.gather(cancel_task, return_exceptions=True)
        return await request_task

    async def _stream_response(
        self, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None
    ) -> dict[str, Any]:
        """Stream response from LLM."""
        if self.agent.streaming_handler is None:
            raw = await self.agent.provider.chat(messages, tools=tools)
            return self._extract_response_data(raw)
        stream = self.agent.provider.stream(messages, tools=tools)
        cancel_event = self.agent.tracker_info._cancel_event if self.agent.tracker_info else None
        result = await self.agent.streaming_handler.handle_stream(stream, cancel_event=cancel_event)
        return result

    def _extract_response_data(self, response: dict[str, Any]) -> dict[str, Any]:
        """Extract content, tool calls, and per-call usage from an API response."""
        usage = normalize_usage(response.get("usage"))
        choices = response.get("choices", [])
        if not choices:
            return {"content": None, "tool_calls": None, "usage": usage}
        message = choices[0].get("message", {})

        return {
            "content": message.get("content"),
            "tool_calls": message.get("tool_calls"),
            "finish_reason": choices[0].get("finish_reason"),
            "reasoning_content": message.get("reasoning_content"),
            "usage": usage,
        }
