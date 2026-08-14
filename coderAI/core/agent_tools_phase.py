"""Tool execution and post-tool completion phase."""

import logging
from typing import TYPE_CHECKING, Any, Optional

from coderAI.core.loop_guard import LoopGuard, doom_message
from coderAI.core.ports import AgentRuntime, ProgressCallback
from coderAI.core.services import get_services
from coderAI.core.tool_executor import BatchStatus, ToolExecutor
from coderAI.core.turn import TurnContext
from coderAI.system.error_policy import (
    BudgetExceededError,
    MAX_CONSECUTIVE_ERRORS,
    check_budget_limit,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from coderAI.system.history import Session
    from coderAI.system.hooks_manager import HooksManager


class ToolsPhase:
    """Typed mixin contract for the tool side of ``ExecutionLoop``."""

    agent: AgentRuntime
    loop_guard: LoopGuard
    tool_executor: ToolExecutor
    hooks_manager: HooksManager
    progress_callback: Optional[ProgressCallback]
    _turn: TurnContext

    if TYPE_CHECKING:

        def _session(self) -> Session: ...
        def _refresh_messages_from_session(self, messages: list[dict[str, Any]]) -> None: ...

        def _get_tool_schemas(
            self,
            objective: str = "",
            *,
            warm_tool_names: Optional[set[str]] = None,
        ) -> Optional[list[dict[str, Any]]]: ...

        async def _handle_budget_exceeded(
            self, error: BudgetExceededError
        ) -> dict[str, Any]: ...

        async def _handle_cancellation(self) -> dict[str, Any]: ...
        async def _handle_fatal_error(self, error: Exception, count: int) -> dict[str, Any]: ...

        async def _finalize_turn(
            self,
            *,
            tail: Optional[str] = None,
            fallback: str = "",
            content_override: Optional[str] = None,
            error: bool = False,
            stop_reason: str = "stop",
            iterations: int = 0,
            run_stop_hooks: bool = True,
            hooks_data: Any = None,
            repair_unpaired: Optional[bool] = None,
        ) -> dict[str, Any]: ...

        async def _call_llm_with_retry(
            self,
            messages: list[dict[str, Any]],
            tool_schemas: Optional[list[dict[str, Any]]],
        ) -> dict[str, Any]: ...

    async def _handle_tools_phase(
        self, state: TurnContext, response_data: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        """Execute the tool calls and post-process the results.

        Returns a final response dict to end the turn, or ``None`` to
        continue with the next iteration.
        """
        content = response_data.get("content")
        tool_calls = response_data.get("tool_calls")

        if not tool_calls:
            if (
                state.tools_were_used
                and not (content or "").strip()
                and state.empty_post_tool_retries == 0
                and state.iteration < state.max_iterations
            ):
                state.empty_post_tool_retries += 1
                msgs = self._session().messages
                if (
                    msgs
                    and msgs[-1].role == "assistant"
                    and not (msgs[-1].content or "").strip()
                    and not msgs[-1].tool_calls
                ):
                    msgs.pop()
                self._refresh_messages_from_session(state.messages)
                state.messages.append(
                    {
                        "role": "system",
                        "content": (
                            "Your response after tool execution was empty. Continue the task "
                            "autonomously without waiting for another user message. If the tool "
                            "started an asynchronous action or did not confirm the requested "
                            "outcome, use the available tools to wait for and verify it. Otherwise, "
                            "provide a concise final response now."
                        ),
                    }
                )
                get_services().events.emit(
                    "agent_status",
                    message="Tool finished without a final response; continuing automatically.",
                )
                return None

            if state.tools_were_used and not (content or "").strip():
                try:
                    summary = await self._post_tool_closing_message(state.user_message)
                except BudgetExceededError:
                    return await self._handle_budget_exceeded(
                        BudgetExceededError("Budget exceeded during closing summary.")
                    )
                if summary:
                    msgs = self._session().messages
                    if (
                        msgs
                        and msgs[-1].role == "assistant"
                        and not (msgs[-1].content or "").strip()
                        and not msgs[-1].tool_calls
                    ):
                        msgs.pop()
                    self._session().add_message("assistant", summary)
                    state.reply_parts.append(summary.strip())

            gate_result = await self._apply_completion_gate(state, content or "")
            if gate_result is not None:
                return gate_result
            if state.objective_state and state.objective_state.completion_status == "incomplete":
                return None

            return await self._finalize_turn(
                fallback=(
                    content
                    or (
                        "The requested tool action completed, but no final details were returned."
                        if state.tools_were_used
                        else ""
                    )
                ),
                stop_reason="stop",
                iterations=state.iteration,
                hooks_data=state.hooks_data,
            )

        in_batch_doom = self.loop_guard.detect_in_batch(tool_calls)
        if in_batch_doom is not None:
            doom_msg = doom_message(*in_batch_doom)
            get_services().events.emit("agent_warning", message=doom_msg)
            return await self._finalize_turn(
                tail=doom_msg,
                error=True,
                stop_reason="doom_loop",
                iterations=state.iteration,
                hooks_data=state.hooks_data,
            )

        outcome = await self.tool_executor.orchestrate_tool_calls(
            tool_calls,
            state.messages,
            state.user_message,
            state.hooks_data,
            self.hooks_manager,
            turn=state,
        )

        # Re-route after every batch. Successful tools stay warm for this
        # objective, while a mid-turn Plan Mode amendment immediately removes
        # any mutating schemas regardless of warmth.
        state.tool_schemas = self._get_tool_schemas(
            state.user_message,
            warm_tool_names=state.warm_tool_names,
        )
        state.routed_tool_names = {
            str((schema.get("function") or {}).get("name"))
            for schema in state.tool_schemas or []
            if (schema.get("function") or {}).get("name")
        }

        called_tool_names = {
            (call.get("function") or {}).get("name")
            for call in tool_calls
            if isinstance(call, dict)
        }
        if called_tool_names & {"mcp_connect", "mcp_disconnect"}:
            # MCP topology changes alter both function schemas and the dynamic
            # prompt appendix. Refresh both before the next model iteration.
            self.agent._tool_schemas_dirty = True
            self.agent._cached_system_prompt = None
            self.agent._refresh_session_system_prompt()
            self._refresh_messages_from_session(state.messages)

        # Emit progress after tool execution for sub-agent streaming
        if self.progress_callback:
            try:
                self.progress_callback(tool_calls, outcome.status is not BatchStatus.OK)
            except Exception:
                pass

        # Check for cancellation after tools (long tool chains can be interrupted)
        if self.agent.tracker_info and self.agent.tracker_info.is_cancelled:
            return await self._handle_cancellation()

        if outcome.status is BatchStatus.DOOM_LOOP:
            # Cross-iteration doom-loop hard stop: the executor flagged that some
            # (tool, args) fingerprint has been called too many times. Terminate
            # cleanly with the same lifecycle and message as the in-batch stop.
            return await self._finalize_turn(
                tail=doom_message(outcome.doom_tool or "unknown", outcome.doom_count),
                error=True,
                stop_reason="doom_loop",
                iterations=state.iteration,
                hooks_data=state.hooks_data,
            )
        elif outcome.status is BatchStatus.DENIED:
            # Denials should not count toward consecutive_tool_errors when
            # ``continue_loop_on_deny`` is True (the model can retry with a
            # different approach). When False, treat denial as a terminal stop.
            if not self.agent.config.continue_loop_on_deny:
                names_str = ", ".join(outcome.denied_tools) if outcome.denied_tools else "unknown"
                get_services().events.emit(
                    "agent_warning",
                    message=f"Tool(s) denied by user: {names_str}. Stopping.",
                )
                return await self._finalize_turn(
                    fallback=f"Tool(s) denied: {names_str}",
                    stop_reason="denied",
                    iterations=state.iteration,
                    hooks_data=state.hooks_data,
                )
            # continue_loop_on_deny=True: reset the counter so repeated denials
            # don't look like fatal errors. The executor already updated the
            # transcript so the loop feeds the denial back to the LLM.
            state.tools_were_used = True
            state.consecutive_tool_errors = 0
        elif outcome.status is BatchStatus.RETRY:
            # All tool calls failed (or were unparsable); the executor updated
            # the transcript with error feedback for the next LLM round.
            # Still count as tool use so an empty follow-up can auto-continue
            # instead of silently ending the turn.
            state.tools_were_used = True
            state.consecutive_tool_errors += 1
            if state.consecutive_tool_errors >= MAX_CONSECUTIVE_ERRORS:
                return await self._handle_fatal_error(
                    RuntimeError("Tool execution failed repeatedly."),
                    state.consecutive_tool_errors,
                )
        else:  # BatchStatus.OK
            state.tools_were_used = True
            # A successful tool batch opens a new post-tool reply window, so
            # allow another one-shot empty-response recovery if needed.
            state.empty_post_tool_retries = 0
            state.consecutive_tool_errors = 0

        # Check budget after expensive tool operations (MCP, sub-agents,
        # summarization) that consume tokens through internal LLM calls.
        if self.agent.config.budget_limit > 0:
            check_budget_limit(
                self.agent.config.budget_limit,
                self.agent.cost_tracker,
                emit_warning=True,
            )

        # Manage context window after tool results — gate on meaningful state
        # change to avoid embedding retrieval / compaction on pure read-only
        # batches where nothing mutated or verified.
        should_inject = False
        try:
            # Inject if we have untrusted content, verification progress, or
            # workspace mutations that may affect subsequent reasoning.
            if self._turn.ingested_untrusted or state.objective_state is not None:
                # ObjectiveState tracks mutations/verifications; inject if
                # evidence grew or checks completed this batch
                obj = state.objective_state
                if obj is not None and (len(obj.evidence) > 0 or len(obj.artifacts_changed) > 0):
                    # Only inject when there is at least one mutation or
                    # verification in the recent evidence window
                    recent_kinds = {e.kind for e in obj.evidence[-5:]} if obj.evidence else set()
                    if recent_kinds & {"mutation", "verification"} or obj.checks_completed:
                        should_inject = True
                    elif self._turn.ingested_untrusted:
                        should_inject = True
                elif self._turn.ingested_untrusted:
                    should_inject = True
            # Also inject if the tool batch itself reported workspace changes
            # via the outcome (mutating tools). Check last tool results for
            # _workspace_changes marker.
            if not should_inject and outcome.status is BatchStatus.OK:
                # Inspect recent messages for workspace mutation markers
                for msg in reversed(state.messages[-6:]):
                    if isinstance(msg, dict) and msg.get("role") == "tool":
                        content = msg.get("content", "")
                        if isinstance(content, str) and "_workspace_changes" in content:
                            should_inject = True
                            break
        except Exception:
            should_inject = True
        # Fallback: inject at least every 3 iterations to keep window healthy
        if not should_inject and state.iteration % 3 == 0:
            should_inject = True
        if should_inject:
            state.messages = self.agent.context_controller.inject_context(
                state.messages, query=state.user_message
            )
        return None

    async def _apply_completion_gate(
        self, state: TurnContext, proposed_content: str
    ) -> Optional[dict[str, Any]]:
        """Accept, retry, or reject a model's proposal to finish the turn."""
        objective = state.objective_state
        enabled = self.agent.config.completion_gate_enabled
        if objective is None:
            return None
        if not enabled:
            objective.completion_status = "reasoned"
            return None

        decision = objective.evaluate_completion()
        if decision.allowed:
            return None

        max_retries = self.agent.config.completion_gate_max_retries
        if (
            objective.completion_gate_attempts < max_retries
            and state.iteration < state.max_iterations
        ):
            objective.completion_gate_attempts += 1
            objective.persist()
            messages = self._session().messages
            if messages and messages[-1].role == "assistant" and not messages[-1].tool_calls:
                messages.pop()
            if proposed_content.strip() and state.reply_parts:
                if state.reply_parts[-1] == proposed_content.strip():
                    state.reply_parts.pop()
            feedback = (
                "[Completion Gate]: The runtime rejected the completion proposal. "
                "Resolve the missing evidence before answering finally:\n- "
                + "\n- ".join(decision.issues)
                + "\nRun the narrowest relevant checks after the latest change and inspect each "
                "changed file. If verification is impossible, explain the exact blocker and "
                "remaining risk; the runtime will return an unverified outcome, not success."
            )
            self._session().add_message("system", feedback)
            self._refresh_messages_from_session(state.messages)
            get_services().events.emit(
                "agent_warning",
                message="Completion gate requested missing verification evidence.",
            )
            return None

        objective.mark_unverified(decision.issues)
        note = "Runtime completion status: unverified. " + " ".join(decision.issues)
        return await self._finalize_turn(
            fallback=proposed_content,
            tail=note,
            error=True,
            stop_reason="unverified",
            iterations=state.iteration,
            hooks_data=state.hooks_data,
        )

    async def _post_tool_closing_message(self, user_message: str) -> Optional[str]:
        """Ask once for a short user-visible wrap-up when tools ran but the model returned no final text."""
        closing_prompt = (
            "Tools have finished. Write 1–3 short sentences for the user that state "
            "what was done and the outcome, with concrete details from this turn "
            "(file paths, commands, or errors) when applicable. "
            "If you already gave a full explanation in assistant messages above, add one sentence "
            "that points to that work without repeating it verbatim. "
            "Do not use stock filler or the same generic closing every time. Do not call tools."
        )

        messages = self._session().get_messages_for_api()
        messages = self.agent.context_controller.inject_context(messages, query=user_message)
        messages.append({"role": "user", "content": closing_prompt})
        get_services().events.emit(
            "agent_status",
            message="\nWriting a short completion summary…",
        )
        try:
            response = await self._call_llm_with_retry(messages, None)
        except BudgetExceededError:
            raise
        except Exception as e:
            logger.warning("Post-tool closing message failed: %s", e)
            return None

        text = (response.get("content") or "").strip()
        return text or None
