"""Execution Loop orchestrator for CoderAI agent."""

import asyncio
import json
import logging
import time as _time
from typing import Any, Dict, List, Optional

from .agent_tracker import AgentStatus
from .cost import CostTracker
from .events import event_emitter
from .history import Message
from .tool_executor import ToolExecutor
from .error_policy import (
    BudgetExceededError,
    compute_retry_delay,
    is_transient_error,
    MAX_RETRIES_PER_ITERATION,
    MAX_CONSECUTIVE_ERRORS,
    MAX_CONSECUTIVE_PAUSES,
)

logger = logging.getLogger(__name__)

# Number of consecutive identical tool calls before triggering doom-loop
# detection. OpenCode uses 3; we match that default.
DOOM_LOOP_THRESHOLD = 3

# Injected as a synthetic assistant message when within 5 steps of the max.
MAX_STEPS_WARNING = (
    "<system-reminder>\n"
    "You are approaching the maximum number of iterations ({remaining} remaining). "
    "Prioritize completing the most critical remaining work and provide a final "
    "response to the user. Do not start any new multi-step processes.\n"
    "</system-reminder>"
)

# Injected when the plan tool was used and the agent should reference it.
PLAN_MODE_REMINDER = (
    "<system-reminder>\n"
    "A plan exists for this task. Before making changes, consult the current plan "
    "(use `plan` action='show') to see the current step. Advance the plan with "
    "`plan` action='advance' after completing each step. If the plan is complete, "
    "provide a final summary.\n"
    "</system-reminder>"
)


def _is_plan_active(session) -> bool:
    """Check whether the plan tool was invoked during this session."""
    if session is None:
        return False
    for msg in session.messages:
        if msg.role == "assistant" and msg.tool_calls:
            for tc in msg.tool_calls:
                func = (tc or {}).get("function", {}) or {}
                if func.get("name") == "plan":
                    return True
    return False


def _inject_step_reminders(
    messages: List[Dict[str, Any]],
    iteration: int,
    max_iterations: int,
    session,
) -> List[Dict[str, Any]]:
    """Insert system reminders into the message list for multi-step awareness.

    - On iteration > 1 with plan active: inject a plan reminder.
    - When within 5 steps of max_iterations: inject a step-limit warning.
    """
    result = list(messages)
    reminders: List[str] = []

    if iteration > 1 and _is_plan_active(session):
        reminders.append(PLAN_MODE_REMINDER)

    steps_left = max_iterations - iteration
    if 1 <= steps_left <= 5:
        reminders.append(MAX_STEPS_WARNING.format(remaining=steps_left))

    if reminders:
        combined = "\n\n".join(reminders)
        result.append({"role": "user", "content": combined})

    return result


class ExecutionLoop:
    """Manages the main LLM-Tool interaction loop."""

    def __init__(self, agent, progress_callback=None):
        self.agent = agent
        self.tool_executor = ToolExecutor(agent)
        self.progress_callback = progress_callback
        # Use the agent's hooks manager for consistent state (e.g. approval cache)
        self.hooks_manager = getattr(agent, "hooks_manager", None)
        if self.hooks_manager is None:
            class _NoopHooksManager:
                def load_hooks(self):
                    return None

                async def run_hooks(self, *args, **kwargs):
                    return []

            self.hooks_manager = _NoopHooksManager()
        self._last_repaired_msg_count: int = 0

    async def run(self, user_message: str) -> Dict[str, Any]:
        """Process a user message and return response."""

        # 1. Prepare session and check budget
        budget_block = self._prepare_session(user_message)
        if budget_block:
            return budget_block

        read_cache = getattr(self.agent, "read_cache", None)
        if read_cache is not None:
            read_cache.bump_turn()

        # 2. Run on_user_prompt and chat.message hooks
        hooks_data = self.hooks_manager.load_hooks()
        if hooks_data:
            await self.hooks_manager.run_hooks("*", "on_user_prompt", {"text": user_message}, hooks_data)
            transformed = await getattr(self.hooks_manager, "run_chat_message_hooks", lambda *a, **kw: None)(
                user_message, hooks_data
            )
            if transformed:
                user_message = transformed

        # 3. Persist the user message so the LLM sees what was asked
        self.agent.session.add_message("user", user_message)

        # 4. Prepare messages (retrieve, inject context, manage window)
        messages = await self._prepare_messages(user_message)

        # 5. Get tool schemas
        tool_schemas = self._get_tool_schemas()

        # 6. Load project hooks
        hooks_data = self.hooks_manager.load_hooks()

        self.agent._assistant_reply_parts.clear()
        tools_were_used = False

        # Process with LLM (potentially multiple rounds for tool calls)
        max_iterations = self.agent.config.max_iterations
        if max_iterations <= 0:
            logger.warning(f"max_iterations={max_iterations} is invalid, clamping to 1")
            max_iterations = 1
        iteration = 0
        consecutive_errors = 0
        consecutive_pauses = 0

        while iteration < max_iterations:
            iteration += 1

            if self.agent.tracker_info and self.agent.tracker_info.is_cancelled:
                return await self._handle_cancellation()

            try:
                if self.agent.tracker_info:
                    if self.agent.tracker_info.status != AgentStatus.THINKING:
                        self.agent.tracker_info.status = AgentStatus.THINKING
                        self.agent._sync_tracker()

                # Inject step reminders (plan mode, step-limit warnings)
                step_aware_messages = _inject_step_reminders(
                    messages, iteration, max_iterations, self.agent.session
                )

                response_data = await self._call_llm_with_retry(step_aware_messages, tool_schemas)

                consecutive_errors = 0

                content = response_data.get("content")
                tool_calls = response_data.get("tool_calls")
                finish_reason = response_data.get("finish_reason")

                if content and content.strip():
                    self.agent._assistant_reply_parts.append(content.strip())

                self.agent.session.add_message("assistant", content, tool_calls=tool_calls)

                if finish_reason == "refusal":
                    event_emitter.emit(
                        "agent_warning",
                        message="Model refused this request (stop_reason=refusal). Returning model text without further tool calls."
                    )
                    # Return the refusal content as final response — do NOT loop
                    self.agent._finish_tracker()
                    self.agent.save_session()

                    # Run on_stop hooks
                    if hooks_data:
                        await self.hooks_manager.run_hooks("*", "on_stop", {"iterations": iteration}, hooks_data)

                    joined = "\n\n".join(self.agent._assistant_reply_parts)
                    return {
                        "content": joined if joined else (content or ""),
                        "messages": self.agent.session.messages,
                        "model_info": self.agent.provider.get_model_info(),
                    }
                elif finish_reason == "length":
                    # Model hit max_tokens and was cut off mid-response.
                    event_emitter.emit(
                        "agent_warning",
                        message=(
                            "Response was truncated (max_tokens limit reached). "
                            "Increase max_tokens in config to fix this."
                        ),
                    )
                    self.agent._finish_tracker(error=True)
                    self.agent.save_session()
                    joined = "\n\n".join(self.agent._assistant_reply_parts)
                    note = (
                        "[Output cut off — the model hit the max_tokens limit. "
                        "Run `coderAI config set max_tokens 16000` to increase it.]"
                    )
                    return {
                        "content": f"{joined}\n\n{note}" if joined else note,
                        "messages": self.agent.session.messages,
                        "model_info": self.agent.provider.get_model_info(),
                    }
                elif finish_reason == "pause_turn":
                    # Model paused mid-thought. Re-issue same messages to resume.
                    consecutive_pauses += 1
                    if consecutive_pauses > MAX_CONSECUTIVE_PAUSES:
                        event_emitter.emit(
                            "agent_warning",
                            message=(
                                f"Model returned pause_turn {consecutive_pauses} times in a row; "
                                "aborting to avoid an infinite loop."
                            ),
                        )
                        return await self._handle_max_iterations()
                    event_emitter.emit(
                        "agent_paused",
                        message="Model requested pause_turn; resuming automatically.",
                    )
                    iteration -= 1  # don't count toward max_iterations
                    continue
                else:
                    consecutive_pauses = 0

                if not tool_calls:
                    if tools_were_used and not (content or "").strip() and not self.agent._assistant_reply_parts:
                        try:
                            summary = await self._post_tool_closing_message(user_message)
                        except BudgetExceededError:
                            return self._handle_budget_exceeded(BudgetExceededError("Budget exceeded during closing summary."))
                        if summary:
                            msgs = self.agent.session.messages
                            if (
                                msgs
                                and msgs[-1].role == "assistant"
                                and not (msgs[-1].content or "").strip()
                                and not msgs[-1].tool_calls
                            ):
                                msgs.pop()
                            self.agent.session.add_message("assistant", summary)
                            self.agent._assistant_reply_parts.append(summary.strip())

                    self.agent._finish_tracker()
                    self.agent.save_session()

                    # Run on_stop hooks
                    if hooks_data:
                        await self.hooks_manager.run_hooks("*", "on_stop", {"iterations": iteration}, hooks_data)

                    joined = "\n\n".join(self.agent._assistant_reply_parts)
                    reply_text = joined if joined else (content or "")
                    return {
                        "content": reply_text,
                        "messages": self.agent.session.messages,
                        "model_info": self.agent.provider.get_model_info(),
                    }

                # Doom-loop detection: check if the last DOOM_LOOP_THRESHOLD
                # consecutive assistant messages contain identical tool calls.
                if self._detect_doom_loop(tool_calls):
                    doom_msg = (
                        "The last {n} tool-call steps repeated the same tool + arguments. "
                        "The model appears to be stuck in a loop. Stopping to avoid "
                        "wasting tokens. Please rephrase your request or provide "
                        "additional guidance."
                    ).format(n=DOOM_LOOP_THRESHOLD)
                    event_emitter.emit("agent_warning", message=doom_msg)
                    self.agent._finish_tracker(error=True)
                    self.agent.save_session()
                    joined = "\n\n".join(self.agent._assistant_reply_parts)
                    return {
                        "content": joined + "\n\n" + doom_msg if joined else doom_msg,
                        "messages": self.agent.session.messages,
                        "model_info": self.agent.provider.get_model_info(),
                    }

                did_error, fatal_res = await self.tool_executor.orchestrate_tool_calls(
                    tool_calls, messages, user_message, hooks_data, self.hooks_manager
                )

                # Emit progress after tool execution for sub-agent streaming
                if self.progress_callback:
                    try:
                        self.progress_callback(tool_calls, did_error)
                    except Exception:
                        pass

                # Check for cancellation after tools (long tool chains can be interrupted)
                if self.agent.tracker_info and self.agent.tracker_info.is_cancelled:
                    return await self._handle_cancellation()

                if did_error:
                    # Cross-iteration doom-loop hard stop: the executor
                    # has flagged that some (tool, args) fingerprint has
                    # been called too many times. Terminate cleanly with
                    # the same lifecycle as a normal stop.
                    if (
                        fatal_res
                        and isinstance(fatal_res, dict)
                        and fatal_res.get("_doom_loop_stop")
                    ):
                        tool_name = fatal_res.get("tool_name", "unknown")
                        count = fatal_res.get("count", 0)
                        stop_msg = (
                            f"Stopped to avoid wasting tokens: '{tool_name}' was "
                            f"called {count} times with identical arguments. "
                            "The model appears to be looping. Please rephrase your "
                            "request or provide additional guidance."
                        )
                        self.agent._finish_tracker(error=True)
                        self.agent.save_session()
                        if hooks_data:
                            await self.hooks_manager.run_hooks(
                                "*", "on_stop",
                                {"iterations": iteration, "error": "doom_loop"},
                                hooks_data,
                            )
                        joined = "\n\n".join(self.agent._assistant_reply_parts)
                        return {
                            "content": (joined + "\n\n" + stop_msg) if joined else stop_msg,
                            "messages": self.agent.session.messages,
                            "model_info": self.agent.provider.get_model_info(),
                        }

                    # Detect denied tools vs real errors. Denials should not
                    # count toward consecutive_errors when
                    # ``continue_loop_on_deny`` is True (the model can retry
                    # with a different approach). When False, treat denial as
                    # a terminal stop.
                    has_denials = (
                        fatal_res
                        and isinstance(fatal_res, dict)
                        and bool(fatal_res.get("_denied"))
                    )
                    if has_denials:
                        if not self.agent.config.continue_loop_on_deny:
                            denied_names = fatal_res.get("_denied_tools", [])
                            names_str = ", ".join(denied_names) if denied_names else "unknown"
                            event_emitter.emit(
                                "agent_warning",
                                message=f"Tool(s) denied by user: {names_str}. Stopping.",
                            )
                            self.agent._finish_tracker()
                            self.agent.save_session()
                            joined = "\n\n".join(self.agent._assistant_reply_parts)
                            return {
                                "content": joined or f"Tool(s) denied: {names_str}",
                                "messages": self.agent.session.messages,
                                "model_info": self.agent.provider.get_model_info(),
                            }
                        # continue_loop_on_deny=True: reset counter so
                        # repeated denials don't look like fatal errors.
                        consecutive_errors = 0
                        # {"retry": True} has already been set by the executor
                        # so the loop will feed the denial back to the LLM.
                    else:
                        consecutive_errors += 1
                        # {"retry": True} means the messages were updated with error
                        # feedback and the loop should retry the LLM call — not exit.
                        if fatal_res and fatal_res is not True and fatal_res.get("retry") is not True:
                            return fatal_res
                        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                            return self._handle_fatal_error(
                                RuntimeError("Tool execution failed repeatedly."),
                                consecutive_errors,
                            )
                else:
                    tools_were_used = True
                    consecutive_errors = 0

                # Manage context window after tool results (or error messages) are added
                messages = self.agent.context_controller.inject_context(messages, self.agent.context_manager, query=user_message)
                messages = await self.agent.context_controller.manage_context_window(messages)
            except BudgetExceededError as e:
                # Terminal: budget is a hard stop, not a transient failure.
                return self._handle_budget_exceeded(e)
            except Exception as e:
                logger.error(f"Error during processing: {e}", exc_info=True)
                consecutive_errors += 1

                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    return self._handle_fatal_error(e, consecutive_errors)

                messages = await self._handle_recoverable_error(e, consecutive_errors, user_message)
                continue

        return await self._handle_max_iterations()

    def _prepare_session(self, user_message: str) -> Optional[Dict[str, Any]]:
        """Initialize session and tracker, check budget limits."""
        if self.agent.session is None:
            self.agent.create_session()

        if not self.agent.tracker_info or self.agent.tracker_info.status in (
            AgentStatus.DONE,
            AgentStatus.ERROR,
            AgentStatus.CANCELLED,
        ):
            self.agent._register_tracker(task=user_message[:120])
        else:
            self.agent.tracker_info.current_task = user_message[:120]
            self.agent.tracker_info.status = AgentStatus.THINKING

        if (
            self.agent.config.budget_limit > 0
            and self.agent.cost_tracker.get_total_cost() > self.agent.config.budget_limit
        ):
            msg = f"Budget limit of {CostTracker.format_cost(self.agent.config.budget_limit)} exceeded."
            event_emitter.emit("agent_error", message=msg)
            self.agent._finish_tracker(error=True)
            return {
                "content": f"Blocked: {msg}",
                "messages": self.agent.session.messages if self.agent.session else [],
                "model_info": self.agent.provider.get_model_info(),
            }
        return None

    async def _prepare_messages(self, user_message: str) -> List[Dict[str, Any]]:
        """Retrieve messages from session, inject context, and manage window."""
        session = self.agent.session
        msg_count = len(session.messages) if session else 0
        if msg_count != self._last_repaired_msg_count:
            self._repair_unpaired_tool_calls()
            self._last_repaired_msg_count = msg_count
        messages = self.agent.session.get_messages_for_api()
        messages = self.agent.context_controller.inject_context(
            messages, self.agent.context_manager, query=user_message
        )
        return await self.agent.context_controller.manage_context_window(messages)

    def _detect_doom_loop(self, tool_calls: Optional[list]) -> bool:
        """Check for repetitive identical tool calls indicating a model loop.

        Detects when the model emits the same tool with the same arguments
        ``DOOM_LOOP_THRESHOLD`` times within a single LLM response (i.e. in
        the current tool_calls batch). This matches OpenCode's doom-loop
        detection pattern, which checks parts within the same assistant
        message rather than across iterations.

        When a model goes into a loop across iterations (same tool called
        every step), the executor's ``DUPLICATE_CALL_THRESHOLD`` in
        ``tool_executor.py`` already catches that separately.
        """
        if not tool_calls or len(tool_calls) < DOOM_LOOP_THRESHOLD:
            return False

        fingerprints: Dict[str, int] = {}
        for tc in tool_calls:
            func = tc.get("function", {}) or {}
            name = func.get("name", "") or ""
            args = func.get("arguments")
            fp = json.dumps({"name": name, "args": args}, sort_keys=True, default=str)
            fingerprints[fp] = fingerprints.get(fp, 0) + 1

        max_count = max(fingerprints.values())
        if max_count >= DOOM_LOOP_THRESHOLD:
            logger.warning(
                "Doom loop detected: tool called %d times within a single LLM response",
                max_count,
            )
            return True
        return False

    def _repair_unpaired_tool_calls(self) -> None:
        """Ensure assistant tool calls are followed by matching tool results.

        If a previous iteration crashed after writing an assistant message with
        ``tool_calls`` but before tool result messages were appended, some
        providers reject the next request. We synthesize tool-error messages for
        any missing tool IDs so the transcript remains valid and recoverable.

        Consumes only tool messages whose ``tool_call_id`` matches the current
        assistant's expected IDs to avoid cross-assistant contamination.
        """
        session = self.agent.session
        if not session or not session.messages:
            return

        repaired: List[Message] = []
        injected = 0
        i = 0
        msgs = session.messages
        while i < len(msgs):
            msg = msgs[i]
            repaired.append(msg)

            expected_ids: List[str] = []
            if msg.role == "assistant" and msg.tool_calls:
                for tc in msg.tool_calls:
                    tc_id = (tc or {}).get("id")
                    if isinstance(tc_id, str) and tc_id:
                        expected_ids.append(tc_id)

            if not expected_ids:
                i += 1
                continue

            seen_ids = set()
            j = i + 1
            while j < len(msgs) and msgs[j].role == "tool":
                tcid = msgs[j].tool_call_id
                if tcid in expected_ids:
                    repaired.append(msgs[j])
                    seen_ids.add(tcid)
                j += 1

            missing_ids = [tcid for tcid in expected_ids if tcid not in seen_ids]
            anchor_ts = getattr(msg, "timestamp", None)
            if anchor_ts is None:
                anchor_ts = _time.time()
            for offset, tcid in enumerate(missing_ids, start=1):
                repaired.append(
                    Message(
                        role="tool",
                        content=json.dumps(
                            {
                                "success": False,
                                "error": (
                                    "Tool execution did not complete due to an internal error. "
                                    "Recovered by adding a synthetic tool response."
                                ),
                            }
                        ),
                        tool_call_id=tcid,
                        name="internal_recovery",
                        timestamp=anchor_ts + offset * 1e-6,
                    )
                )
                injected += 1

            i = j

        if injected:
            session.messages = repaired
            session.updated_at = _time.time()
            logger.warning(
                "Recovered %s unpaired assistant tool_call(s) by injecting synthetic tool responses.",
                injected,
            )

    def _get_tool_schemas(self) -> Optional[List[Dict[str, Any]]]:
        """Collect tool schemas from built-in registry and MCP."""
        tool_schemas = self.agent.tools.get_schemas() if self.agent.provider.supports_tools() else None
        try:
            from .tools.mcp import mcp_client
            mcp_schemas = mcp_client.get_tools_as_openai_format()
            if mcp_schemas:
                if tool_schemas is None:
                    tool_schemas = mcp_schemas
                else:
                    tool_schemas = tool_schemas + mcp_schemas
        except Exception as e:
            logger.debug(f"MCP tool discovery skipped: {e}")
        return tool_schemas

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

        messages = self.agent.session.get_messages_for_api()
        messages = self.agent.context_controller.inject_context(
            messages, self.agent.context_manager, query=user_message
        )
        messages = await self.agent.context_controller.manage_context_window(messages)
        messages.append({"role": "user", "content": closing_prompt})
        event_emitter.emit(
            "agent_status",
            message="\n[dim]Writing a short completion summary…[/dim]",
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

    async def _handle_cancellation(self) -> Dict[str, Any]:
        # Run on_stop hooks so cancellation is handled consistently with
        # other terminal paths (refusal, normal stop, max_iterations).
        hooks_data = self.hooks_manager.load_hooks()
        if hooks_data:
            await self.hooks_manager.run_hooks("*", "on_stop", {"iterations": 0, "error": "cancelled"}, hooks_data)

        self.agent._finish_tracker()
        self.agent.save_session()
        joined = "\n\n".join(self.agent._assistant_reply_parts)
        tail = "Agent stopped by user."
        body = f"{joined}\n\n{tail}" if joined else tail
        return {
            "content": body,
            "messages": self.agent.session.messages,
            "model_info": self.agent.provider.get_model_info(),
        }

    def _handle_fatal_error(self, e: Exception, count: int) -> Dict[str, Any]:
        event_emitter.emit("agent_error", message=f"Too many consecutive errors ({count}). Last: {e}")
        self.agent._finish_tracker(error=True)
        self.agent.save_session()
        return {
            "content": f"I encountered {count} consecutive errors. Last error: {e}. Please try again.",
            "messages": self.agent.session.messages,
            "model_info": self.agent.provider.get_model_info(),
        }

    async def _handle_recoverable_error(self, e: Exception, count: int, user_message: str) -> List[Dict[str, Any]]:
        # Sanitize error message to avoid leaking sensitive info (API keys, tracebacks)
        error_str = str(e)
        # Truncate long error messages and strip potential key/token patterns
        if len(error_str) > 200:
            error_str = error_str[:200] + "..."
        import re
        error_str = re.sub(r'(sk-|key-|token-|Bearer\s+|x-api-key[=:]\s*|Authorization:\s*Bearer\s+)[A-Za-z0-9_\-]{8,}', r'\1[REDACTED]', error_str, flags=re.IGNORECASE)

        event_emitter.emit("agent_error", message=f"Error (attempt {count}/{MAX_CONSECUTIVE_ERRORS}): {error_str}")
        messages = self.agent.session.get_messages_for_api()
        messages = self.agent.context_controller.inject_context(messages, self.agent.context_manager, query=user_message)
        messages.append({
            "role": "system",
            "content": (
                f"[System Error Feedback: {error_str}.] "
                "Do NOT retry the exact same tool call with the same arguments — "
                "that will fail the same way. Either change the arguments, use a "
                "different tool, or explain why you cannot proceed."
            ),
        })
        return await self.agent.context_controller.manage_context_window(messages)

    def _handle_budget_exceeded(self, e: BudgetExceededError) -> Dict[str, Any]:
        """Stop the loop cleanly when the budget has been exhausted."""
        event_emitter.emit("agent_error", message=str(e))
        self.agent._finish_tracker(error=True)
        self.agent.save_session()
        return {
            "content": f"Blocked: {e}",
            "messages": self.agent.session.messages if self.agent.session else [],
            "model_info": self.agent.provider.get_model_info(),
        }

    async def _handle_max_iterations(self) -> Dict[str, Any]:
        """Handle hitting the iteration limit."""
        msg = "I've reached the maximum number of iterations. Please try again."
        event_emitter.emit("agent_warning", message=msg)
        self.agent._finish_tracker(error=True)
        self.agent.save_session()

        # Run on_stop hooks
        hooks_data = self.hooks_manager.load_hooks()
        if hooks_data:
            await self.hooks_manager.run_hooks("*", "on_stop", {"iterations": self.agent.config.max_iterations, "error": "max_iterations"}, hooks_data)

        return {
            "content": msg,
            "messages": self.agent.session.messages,
            "model_info": self.agent.provider.get_model_info(),
        }

    async def _call_llm_with_retry(
        self,
        messages: List[Dict[str, Any]],
        tool_schemas: Optional[List[Dict[str, Any]]],
    ) -> Dict[str, Any]:
        """Call the LLM with retry logic for transient errors."""
        provider_messages = self.agent.context_controller.strip_internal_markers(messages)
        for attempt in range(1, MAX_RETRIES_PER_ITERATION + 1):
            try:
                if self.agent.streaming:
                    result = await self._stream_response(provider_messages, tool_schemas)
                else:
                    raw = await self.agent.provider.chat(provider_messages, tools=tool_schemas)
                    result = self._extract_response_data(raw)

                # Update tokens and cost
                model_info = self.agent.provider.get_model_info()
                new_in = model_info.get("total_input_tokens", 0) - self.agent.total_prompt_tokens
                new_out = model_info.get("total_output_tokens", 0) - self.agent.total_completion_tokens

                if new_in < 0 or new_out < 0:
                    logger.warning("Token counters appear to have reset (negative delta). Realigning agent counters to provider.")
                    self.agent.total_prompt_tokens = model_info.get("total_input_tokens", 0)
                    self.agent.total_completion_tokens = model_info.get("total_output_tokens", 0)
                    self.agent.total_tokens = model_info.get("total_tokens", 0)
                else:
                    if new_in > 0 or new_out > 0:
                        self.agent.total_prompt_tokens = model_info.get("total_input_tokens", 0)
                        self.agent.total_completion_tokens = model_info.get("total_output_tokens", 0)
                        self.agent.total_tokens = model_info.get("total_tokens", 0)
                    model_for_cost = getattr(self.agent.provider, "actual_model", self.agent.model)
                    if new_in > 0 or new_out > 0:
                        self.agent.cost_tracker.add_cost(model_for_cost, new_in, new_out)

                    if (
                        self.agent.config.budget_limit > 0
                        and self.agent.cost_tracker.get_total_cost() > self.agent.config.budget_limit
                    ):
                        msg = (
                            f"Budget limit of {CostTracker.format_cost(self.agent.config.budget_limit)} exceeded "
                            f"(current: {CostTracker.format_cost(self.agent.cost_tracker.get_total_cost())}). Stopping."
                        )
                        event_emitter.emit("agent_warning", message=f"BUDGET LIMIT EXCEEDED! {msg}")
                        raise BudgetExceededError(msg)

                return result
            except BudgetExceededError:
                # Never retry a budget failure — it's a hard stop, not a blip.
                raise
            except Exception as e:
                if not is_transient_error(e) or attempt == MAX_RETRIES_PER_ITERATION:
                    raise
                delay = compute_retry_delay(e, attempt)
                logger.warning(
                    f"Transient error (attempt {attempt}/{MAX_RETRIES_PER_ITERATION}): "
                    f"{e}. Retrying in {delay:.1f}s…"
                )
                event_emitter.emit(
                    "agent_warning",
                    message=f"Transient error, retrying in {delay:.1f}s… ({attempt}/{MAX_RETRIES_PER_ITERATION})",
                )
                await asyncio.sleep(delay)

        raise RuntimeError("_call_llm_with_retry exhausted without returning or raising")

    async def _stream_response(
        self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """Stream response from LLM."""
        if self.agent.streaming_handler is None:
            raw = await self.agent.provider.chat(messages, tools=tools)
            return self._extract_response_data(raw)
        stream = self.agent.provider.stream(messages, tools=tools)
        result = await self.agent.streaming_handler.handle_stream(stream)
        return result

    def _extract_response_data(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """Extract content and tool calls from API response."""
        choices = response.get("choices", [])
        if not choices:
            return {"content": None, "tool_calls": None}
        message = choices[0].get("message", {})

        return {
            "content": message.get("content"),
            "tool_calls": message.get("tool_calls"),
            "finish_reason": choices[0].get("finish_reason"),
        }
