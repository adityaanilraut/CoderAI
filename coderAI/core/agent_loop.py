"""Execution Loop orchestrator for CoderAI agent."""

import asyncio
import json
import logging
import time as _time
from typing import Any, Dict, List, Optional

from coderAI.core.agent_tracker import AgentStatus
from coderAI.system.cost import CostTracker
from coderAI.system.events import event_emitter
from coderAI.system.history import Message
from coderAI.core.tool_executor import ToolExecutor
from coderAI.system.error_policy import (
    BudgetExceededError,
    check_budget_limit,
    compute_iteration_backoff,
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

# Upper-bound fallback constant used by ExecutionLoop when
# ``agent.config.max_iterations_hard_cap`` is not set on a config instance.
# The authoritative value lives in ``coderAI.system.config.Config``.
MAX_ITERATIONS_HARD_CAP = 200

# Prefix used to tag synthetic system messages persisted into the session
# after an unexpected recoverable error. The context controller and the
# sub-agent bootstrap recognise this marker to preserve / propagate the
# error feedback across iterations and into spawned sub-agents.
RECOVERABLE_ERROR_MARKER = "[Recoverable Error]:"


class ExecutionLoop:
    """Manages the main LLM-Tool interaction loop."""

    def __init__(self, agent, progress_callback=None):
        self.agent = agent
        self.tool_executor = ToolExecutor(agent)
        self.progress_callback = progress_callback
        # Use the agent's hooks manager for consistent state (e.g. approval cache)
        hooks_manager = getattr(agent, "hooks_manager", None)
        if hooks_manager is None:

            class _NoopHooksManager:
                def load_hooks(self):
                    return None

                async def run_hooks(self, *args, **kwargs):
                    return []

            hooks_manager = _NoopHooksManager()
        self.hooks_manager: Any = hooks_manager
        self._last_repaired_msg_count: int = 0
        # Turn-scoped flags. ``run()`` resets these at the top of each call so
        # state never leaks across user messages.
        self._plan_reminder_emitted: bool = False
        self._last_plan_step: Optional[int] = None
        self._length_retry_used: bool = False
        self._hard_cap_warned: bool = False
        self._health_check_counter: int = 0

    def _read_active_plan_step(self) -> Optional[Dict[str, Any]]:
        """Return ``{current_step, total_steps, description}`` for the on-disk
        plan, or ``None`` when no plan is active.

        Reads ``.coderAI/current_plan.json`` via the shared ``read_current_plan``
        utility.
        """
        try:
            from coderAI.system.project_layout import read_current_plan

            project_root = str(getattr(self.agent.config, "project_root", "."))
            plan = read_current_plan(project_root)
            if not plan:
                return None
            steps = plan.get("steps") or []
            total = len(steps)
            if total == 0:
                return None
            current = int(plan.get("current_step", 0) or 0)
            if current < 0:
                current = 0
            if current >= total:
                desc = "All plan steps complete"
            else:
                desc = str(steps[current].get("description") or "")
            return {"current_step": current, "total_steps": total, "description": desc}
        except Exception:
            return None

    def _inject_step_reminders(
        self,
        messages: List[Dict[str, Any]],
        iteration: int,
        max_iterations: int,
    ) -> List[Dict[str, Any]]:
        """Append a single ``system`` reminder combining plan progress and the
        approaching-step-limit warning, when applicable.

        The plan reminder is emitted at most once per turn and re-emitted only
        when the on-disk plan's ``current_step`` has advanced since the last
        injection (tracked via ``self._plan_reminder_emitted`` /
        ``self._last_plan_step``). The step-limit hint is emitted on every
        iteration that falls inside the 5-step window so the model can see
        the budget shrink in real time.
        """
        result = list(messages)
        parts: List[str] = []

        plan_info = self._read_active_plan_step()
        if plan_info is not None:
            current_step = plan_info["current_step"]
            total_steps = plan_info["total_steps"]
            description = plan_info["description"]
            advanced = self._last_plan_step is not None and current_step != self._last_plan_step
            if (not self._plan_reminder_emitted) or advanced:
                self._plan_reminder_emitted = True
                self._last_plan_step = current_step
                if current_step >= total_steps:
                    parts.append(
                        "A plan is active and every step is marked complete "
                        "(use `plan` action='show' to confirm). If the work is "
                        "really done, give the user a final summary; otherwise "
                        "open a new plan or fix the remaining gaps."
                    )
                else:
                    parts.append(
                        f"A plan is active. Currently on step {current_step + 1} "
                        f'of {total_steps}: "{description}". Consult it with '
                        f"`plan` action='show' before changing course and "
                        f"advance it with `plan` action='advance' once the step "
                        f"is finished."
                    )

        steps_left = max_iterations - iteration
        if 1 <= steps_left <= 5:
            parts.append(
                f"You are approaching the maximum number of iterations "
                f"({steps_left} remaining). Prioritize completing the most "
                f"critical remaining work and provide a final response to the "
                f"user. Do not start any new multi-step processes."
            )

        if parts:
            combined = "<system-reminder>\n" + "\n\n".join(parts) + "\n</system-reminder>"
            result.append({"role": "system", "content": combined})
        return result

    def _refresh_messages_from_session(self, messages: List[Dict[str, Any]]) -> None:
        """Replace the in-memory message list with the session transcript."""
        messages.clear()
        if self.agent.session is not None:
            messages.extend(self.agent.session.get_messages_for_api())

    async def run(self, user_message: str) -> Dict[str, Any]:
        """Process a user message and return response."""

        # Reset turn-scoped flags so state never leaks across user messages.
        self._plan_reminder_emitted = False
        self._last_plan_step = None
        self._length_retry_used = False
        self._hard_cap_warned = False

        # Auto-connect MCP servers on first run
        if not getattr(self.agent, "_mcp_initialized", False):
            self.agent._mcp_initialized = True
            await self._autoconnect_mcp_servers()

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
            await self.hooks_manager.run_hooks(
                "*", "on_user_prompt", {"text": user_message}, hooks_data
            )

            async def fallback_chat_hook(*a, **kw):
                return None

            func = getattr(self.hooks_manager, "run_chat_message_hooks", fallback_chat_hook)
            transformed = await func(user_message, hooks_data)
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
        hard_cap = getattr(self.agent.config, "max_iterations_hard_cap", MAX_ITERATIONS_HARD_CAP)
        if max_iterations <= 0:
            logger.warning(f"max_iterations={max_iterations} is invalid, clamping to 1")
            max_iterations = 1
        elif max_iterations > hard_cap:
            clamp_msg = (
                f"max_iterations={max_iterations} exceeds hard cap {hard_cap}; "
                "clamping. Raise `max_iterations_hard_cap` in config if a higher "
                "ceiling is intentional."
            )
            logger.warning(clamp_msg)
            if not self._hard_cap_warned:
                self._hard_cap_warned = True
                event_emitter.emit("agent_warning", message=clamp_msg)
            max_iterations = hard_cap
        iteration = 0
        consecutive_llm_errors = 0
        consecutive_tool_errors = 0
        consecutive_pauses = 0

        while iteration < max_iterations:
            iteration += 1

            # Per-iteration back-off after recoverable errors so retries are
            # paced rather than burned in milliseconds. Cancellation-aware:
            # ``cancel_event.wait()`` short-circuits the sleep when the user
            # hits /cancel.
            delay = compute_iteration_backoff(max(consecutive_llm_errors, consecutive_tool_errors))
            if delay > 0:
                cancel_event = (
                    self.agent.tracker_info._cancel_event if self.agent.tracker_info else None
                )
                event_emitter.emit(
                    "agent_status",
                    message=(
                        f"Backing off {delay:.1f}s after {max(consecutive_llm_errors, consecutive_tool_errors)} consecutive error(s)…"
                    ),
                )
                if cancel_event is not None:
                    try:
                        await asyncio.wait_for(cancel_event.wait(), timeout=delay)
                        # cancel_event fired during back-off → user cancelled.
                        return await self._handle_cancellation()
                    except asyncio.TimeoutError:
                        # Back-off elapsed; continue normally.
                        pass
                else:
                    await asyncio.sleep(delay)

            if self.agent.tracker_info and self.agent.tracker_info.is_cancelled:
                return await self._handle_cancellation()

            try:
                if self.agent.tracker_info:
                    if self.agent.tracker_info.status != AgentStatus.THINKING:
                        self.agent.tracker_info.status = AgentStatus.THINKING
                        self.agent._sync_tracker()

                # Inject step reminders (plan mode, step-limit warnings)
                step_aware_messages = self._inject_step_reminders(
                    messages, iteration, max_iterations
                )

                # Periodic MCP server health check (every 10 iterations)
                self._health_check_counter += 1
                if self._health_check_counter >= 10:
                    self._health_check_counter = 0
                    try:
                        from coderAI.tools.mcp import mcp_client

                        await mcp_client.check_server_health()
                        await mcp_client.auto_reconnect_degraded()
                        tool_schemas = self._get_tool_schemas()
                    except Exception as e:
                        logger.debug("MCP health check failed: %s", e)

                response_data = await self._call_llm_with_retry(step_aware_messages, tool_schemas)

                # One-shot recovery when the model gets cut off mid-tool-loop:
                # ask once for a concise final answer and re-issue the call.
                # Second consecutive ``length`` is terminal (handled below).
                if (
                    response_data.get("finish_reason") == "length"
                    and tools_were_used
                    and not self._length_retry_used
                ):
                    self._length_retry_used = True
                    event_emitter.emit(
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
                    response_data = await self._call_llm_with_retry(
                        step_aware_messages, tool_schemas
                    )

                consecutive_llm_errors = 0

                content = response_data.get("content")
                tool_calls = response_data.get("tool_calls")
                finish_reason = response_data.get("finish_reason")

                if finish_reason == "cancelled":
                    return await self._handle_cancellation()

                if content and content.strip():
                    self.agent._assistant_reply_parts.append(content.strip())

                reasoning_content = response_data.get("reasoning_content")
                session_content = content if content and str(content).strip() else None
                self.agent.session.add_message(
                    "assistant",
                    session_content,
                    tool_calls=tool_calls,
                    reasoning_content=reasoning_content,
                )
                self._refresh_messages_from_session(messages)

                if finish_reason == "refusal":
                    event_emitter.emit(
                        "agent_warning",
                        message="Model refused this request (stop_reason=refusal). Returning model text without further tool calls.",
                    )
                    # Return the refusal content as final response — do NOT loop
                    self.agent._finish_tracker()
                    self.agent.save_session()

                    # Run on_stop hooks
                    if hooks_data:
                        await self.hooks_manager.run_hooks(
                            "*", "on_stop", {"iterations": iteration}, hooks_data
                        )

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
                    if (
                        tools_were_used
                        and not (content or "").strip()
                        and not self.agent._assistant_reply_parts
                    ):
                        try:
                            summary = await self._post_tool_closing_message(user_message)
                        except BudgetExceededError:
                            return self._handle_budget_exceeded(
                                BudgetExceededError("Budget exceeded during closing summary.")
                            )
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
                        await self.hooks_manager.run_hooks(
                            "*", "on_stop", {"iterations": iteration}, hooks_data
                        )

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
                                "*",
                                "on_stop",
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
                    # count toward consecutive_tool_errors when
                    # ``continue_loop_on_deny`` is True (the model can retry
                    # with a different approach). When False, treat denial as
                    # a terminal stop.
                    has_denials = (
                        fatal_res and isinstance(fatal_res, dict) and bool(fatal_res.get("_denied"))
                    )
                    if has_denials and isinstance(fatal_res, dict):
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
                        consecutive_tool_errors = 0
                        # {"retry": True} has already been set by the executor
                        # so the loop will feed the denial back to the LLM.
                    else:
                        consecutive_tool_errors += 1
                        # {"retry": True} means the messages were updated with error
                        # feedback and the loop should retry the LLM call — not exit.
                        if (
                            fatal_res
                            and fatal_res is not True
                            and fatal_res.get("retry") is not True
                        ):
                            return fatal_res  # type: ignore[no-any-return]
                        if consecutive_tool_errors >= MAX_CONSECUTIVE_ERRORS:
                            return self._handle_fatal_error(
                                RuntimeError("Tool execution failed repeatedly."),
                                consecutive_tool_errors,
                            )
                else:
                    tools_were_used = True
                    consecutive_tool_errors = 0

                # Check budget after expensive tool operations (MCP, sub-agents,
                # summarization) that consume tokens through internal LLM calls.
                if self.agent.config.budget_limit > 0:
                    check_budget_limit(
                        self.agent.config.budget_limit,
                        self.agent.cost_tracker,
                        emit_warning=True,
                    )

                # Manage context window after tool results (or error messages) are added
                messages = self.agent.context_controller.inject_context(
                    messages, self.agent.context_manager, query=user_message
                )
                messages = await self.agent.context_controller.manage_context_window(messages)
            except BudgetExceededError as e:
                # Terminal: budget is a hard stop, not a transient failure.
                return self._handle_budget_exceeded(e)
            except Exception as e:
                logger.error(f"Error during processing: {e}", exc_info=True)
                consecutive_llm_errors += 1

                if consecutive_llm_errors >= MAX_CONSECUTIVE_ERRORS:
                    return self._handle_fatal_error(e, consecutive_llm_errors)

                messages = await self._handle_recoverable_error(
                    e, consecutive_llm_errors, user_message
                )
                continue

        return await self._handle_max_iterations()

    async def _autoconnect_mcp_servers(self):
        """Auto-connect configured MCP servers from ~/.coderAI/mcp_servers.json."""
        from pathlib import Path

        mcp_servers_file = Path.home() / ".coderAI" / "mcp_servers.json"
        if not mcp_servers_file.exists():
            return
        try:
            with open(mcp_servers_file, "r") as f:
                data = json.load(f)
            servers = data.get("mcpServers", {})
            if not servers:
                return
            from coderAI.tools.mcp import mcp_client

            for name, config in servers.items():
                if name in mcp_client.servers:
                    continue  # Already connected
                transport = config.get("transport", "stdio")
                if transport == "sse":
                    url = config.get("url")
                    if url:
                        logger.info("Auto-connecting MCP server %s via SSE...", name)
                        res = await mcp_client.connect_sse(name, url)
                        if not res.get("success"):
                            logger.error(
                                "Failed to auto-connect MCP server %s: %s", name, res.get("error")
                            )
                else:
                    command = config.get("command")
                    args = config.get("args", [])
                    if command:
                        logger.info("Auto-connecting MCP server %s via stdio...", name)
                        res = await mcp_client.connect_stdio(name, command, args)
                        if not res.get("success"):
                            logger.error(
                                "Failed to auto-connect MCP server %s: %s", name, res.get("error")
                            )
        except Exception as e:
            logger.error("Error auto-connecting MCP servers: %s", e)

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
        return await self.agent.context_controller.manage_context_window(messages)  # type: ignore[no-any-return]

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
        tool_schemas = (
            self.agent.tools.get_schemas() if self.agent.provider.supports_tools() else None
        )
        try:
            from .tools.mcp import mcp_client

            mcp_schemas = mcp_client.get_tools_as_openai_format()
            if mcp_schemas:
                degraded_servers = {
                    name for name, info in mcp_client.servers.items() if info.get("degraded")
                }
                if degraded_servers:
                    mcp_schemas = [
                        s
                        for s in mcp_schemas
                        if not any(
                            s.get("function", {}).get("name", "").startswith(f"mcp__{srv}__")
                            for srv in degraded_servers
                        )
                    ]
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
            await self.hooks_manager.run_hooks(
                "*", "on_stop", {"iterations": 0, "error": "cancelled"}, hooks_data
            )

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
        event_emitter.emit(
            "agent_error", message=f"Too many consecutive errors ({count}). Last: {e}"
        )
        self.agent._finish_tracker(error=True)
        self.agent.save_session()
        return {
            "content": f"I encountered {count} consecutive errors. Last error: {e}. Please try again.",
            "messages": self.agent.session.messages,
            "model_info": self.agent.provider.get_model_info(),
        }

    async def _handle_recoverable_error(
        self, e: Exception, count: int, user_message: str
    ) -> List[Dict[str, Any]]:
        # Sanitize error message to avoid leaking sensitive info (API keys, tracebacks)
        error_str = str(e)
        # Truncate long error messages and strip potential key/token patterns
        if len(error_str) > 200:
            error_str = error_str[:200] + "..."
        import re

        error_str = re.sub(
            r"(sk-|key-|token-|Bearer\s+|x-api-key[=:]\s*|Authorization:\s*Bearer\s+)[A-Za-z0-9_\-]{8,}",
            r"\1[REDACTED]",
            error_str,
            flags=re.IGNORECASE,
        )

        event_emitter.emit(
            "agent_error", message=f"Error (attempt {count}/{MAX_CONSECUTIVE_ERRORS}): {error_str}"
        )
        self._repair_unpaired_tool_calls()
        self._last_repaired_msg_count = (
            len(self.agent.session.messages) if self.agent.session else 0
        )

        # Persist the recovery feedback into the session so it survives the
        # next ``messages.clear(); messages.extend(session.get_messages_for_api())``
        # cycle in the tool executor. The ``RECOVERABLE_ERROR_MARKER`` prefix
        # lets the context controller (and downstream sub-agents) recognise
        # and preserve these notes across summarization.
        feedback = (
            f"{RECOVERABLE_ERROR_MARKER} {error_str}. "
            "Do NOT retry the exact same tool call with the same arguments — "
            "that will fail the same way. Either change the arguments, use a "
            "different tool, or explain why you cannot proceed."
        )
        if self.agent.session is not None:
            self.agent.session.add_message("system", feedback)
            messages = self.agent.session.get_messages_for_api()
        else:
            messages = [{"role": "system", "content": feedback}]
        messages = self.agent.context_controller.inject_context(
            messages, self.agent.context_manager, query=user_message
        )
        return await self.agent.context_controller.manage_context_window(messages)  # type: ignore[no-any-return]

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
            await self.hooks_manager.run_hooks(
                "*",
                "on_stop",
                {"iterations": self.agent.config.max_iterations, "error": "max_iterations"},
                hooks_data,
            )

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
        provider_messages = self.agent.provider.clean_messages(provider_messages)
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
                new_out = (
                    model_info.get("total_output_tokens", 0) - self.agent.total_completion_tokens
                )

                if new_in < 0 or new_out < 0:
                    logger.warning(
                        "Token counters appear to have reset (negative delta). Realigning agent counters to provider."
                    )
                    self.agent.total_prompt_tokens = model_info.get("total_input_tokens", 0)
                    self.agent.total_completion_tokens = model_info.get("total_output_tokens", 0)
                    self.agent.total_tokens = model_info.get("total_tokens", 0)
                else:
                    if new_in > 0 or new_out > 0:
                        self.agent.total_prompt_tokens = model_info.get("total_input_tokens", 0)
                        self.agent.total_completion_tokens = model_info.get(
                            "total_output_tokens", 0
                        )
                        self.agent.total_tokens = model_info.get("total_tokens", 0)
                    model_for_cost = getattr(self.agent.provider, "actual_model", self.agent.model)
                    if new_in > 0 or new_out > 0:
                        await self.agent.cost_tracker.add_cost(model_for_cost, new_in, new_out)

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
        cancel_event = self.agent.tracker_info._cancel_event if self.agent.tracker_info else None
        result = await self.agent.streaming_handler.handle_stream(stream, cancel_event=cancel_event)
        return result  # type: ignore[no-any-return]

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
            "reasoning_content": message.get("reasoning_content"),
        }
