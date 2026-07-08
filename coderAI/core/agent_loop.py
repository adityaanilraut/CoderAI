"""Execution Loop orchestrator for CoderAI agent."""

import asyncio
import json
import logging
import time as _time
from typing import Any, Dict, List, Optional, Set

from coderAI.core.agent_tracker import AgentStatus
from coderAI.llm.base import normalize_usage
from coderAI.system.cost import CostTracker
from coderAI.system.history import Message
from coderAI.core.services import get_services
from coderAI.core.loop_guard import (
    IN_BATCH_DOOM_THRESHOLD,
    LoopGuard,
    doom_message,
)
from coderAI.core.tool_executor import BatchStatus, ToolExecutor
from coderAI.core.turn import TurnContext
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
from coderAI.system.safeguards import sanitize_for_log

logger = logging.getLogger(__name__)

# Backwards-compatible alias: the in-batch doom threshold now lives in
# ``core.loop_guard``. Re-exported here because tests import it from this module.
DOOM_LOOP_THRESHOLD = IN_BATCH_DOOM_THRESHOLD

# Upper-bound fallback constant used by ExecutionLoop when
# ``agent.config.max_iterations_hard_cap`` is not set on a config instance.
# The authoritative value lives in ``coderAI.system.config.Config``.
MAX_ITERATIONS_HARD_CAP = 200

# Prefix used to tag synthetic system messages persisted into the session
# after an unexpected recoverable error. The context controller and the
# sub-agent bootstrap recognise this marker to preserve / propagate the
# error feedback across iterations and into spawned sub-agents.
RECOVERABLE_ERROR_MARKER = "[Recoverable Error]:"

# Sentinels returned by ``ExecutionLoop._handle_finish_reason`` to steer the
# iteration: continue into the tool phase, or restart the loop without
# consuming an iteration (pause_turn).
_PROCEED_TO_TOOLS = object()
_RESTART_ITERATION = object()


class ExecutionLoop:
    """Manages the main LLM-Tool interaction loop."""

    def __init__(self, agent, progress_callback=None):
        self.agent = agent
        # One doom-loop guard per turn, shared with the executor so the in-batch
        # (loop-side) and cross-iteration (executor-side) detectors agree on
        # fingerprints, thresholds, and the stop message (Phase 2.2).
        self.loop_guard = LoopGuard()
        self.tool_executor = ToolExecutor(agent, self.loop_guard)
        # The per-turn state object, created in ``run()`` and shared with the
        # tool executor. Terminal handlers read ``reply_parts`` off it.
        self._turn: TurnContext = TurnContext()
        self.progress_callback = progress_callback
        # Use the agent's hooks manager for consistent state (e.g. approval cache)
        self.hooks_manager: Any = agent.hooks_manager
        self._last_repaired_msg_count: int = 0
        # Turn-scoped flags. ``run()`` resets these at the top of each call so
        # state never leaks across user messages.
        self._plan_reminder_emitted: bool = False
        self._last_plan_step: Optional[int] = None
        self._length_retry_used: bool = False
        self._hard_cap_warned: bool = False
        self._health_check_counter: int = 0
        # Background MCP health check (see ``_maybe_start_mcp_health_check``).
        # The probes and reconnect back-offs must never run on the LLM loop's
        # critical path, so they run detached; the task sets
        # ``_tool_schemas_dirty`` when servers change so the loop rebuilds
        # schemas on its own thread before the next call.
        self._mcp_health_task: Optional["asyncio.Task[None]"] = None
        self._tool_schemas_dirty: bool = False

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
        if self.agent.session is None:
            return
        messages.clear()
        messages.extend(self.agent.session.get_messages_for_api())

    async def run(self, user_message: str) -> Dict[str, Any]:
        """Process a user message and return response."""

        # Reset turn-scoped flags so state never leaks across user messages.
        self._plan_reminder_emitted = False
        self._last_plan_step = None
        self._length_retry_used = False
        self._hard_cap_warned = False

        # Auto-connect MCP servers on first run
        if not self.agent._mcp_initialized:
            self.agent._mcp_initialized = True
            await self._autoconnect_mcp_servers()

        # 1. Prepare session and check budget
        budget_block = self._prepare_session(user_message)
        if budget_block:
            return budget_block

        read_cache = getattr(self.agent, "read_cache", None)
        if read_cache is not None:
            read_cache.bump_turn()

        # 1b. First-run workspace-trust gate — decide trust before any hook or
        # project-config surface is honoured this turn.
        await self._ensure_workspace_trust()

        # 2. Run on_user_prompt and chat.message hooks
        hooks_data = self.hooks_manager.load_hooks()
        if hooks_data:
            await self.hooks_manager.run_hooks(
                "*", "on_user_prompt", {"text": user_message}, hooks_data
            )

            async def fallback_chat_hook(*a: Any, **kw: Any):
                return None

            func = getattr(self.hooks_manager, "run_chat_message_hooks", fallback_chat_hook)
            transformed = await func(user_message, hooks_data)
            if transformed:
                user_message = transformed

        # 3. Persist the user message so the LLM sees what was asked
        self.agent.session.add_message("user", user_message)

        # 4. Prepare messages (retrieve, inject context, manage window)
        messages = await self._prepare_messages(user_message)

        tool_schemas = self._get_tool_schemas()

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
                get_services().events.emit("agent_warning", message=clamp_msg)
            max_iterations = hard_cap
        state = TurnContext(
            user_message=user_message,
            messages=messages,
            tool_schemas=tool_schemas,
            hooks_data=hooks_data,
            max_iterations=max_iterations,
        )
        self._turn = state

        while state.iteration < state.max_iterations:
            state.iteration += 1
            result = await self._run_iteration(state)
            if result is not None:
                return result

        return await self._handle_max_iterations()

    # ── Workspace-trust gate (Phase 2.3) ────────────────────────────────────

    async def _ensure_workspace_trust(self) -> None:
        """First-run trust decision for the current workspace.

        Runs once per agent. If the project root carries a ``.coderAI``
        execution surface and is not yet trusted, prompt the user. Fail-closed:
        no interactive path (headless / piped) or a decline leaves the
        workspace untrusted, so hooks stay off and the ``config.json`` overlay
        stays skipped. Any error is treated as untrusted.
        """
        if self.agent._workspace_trust_checked:
            return
        self.agent._workspace_trust_checked = True
        try:
            from coderAI.system.trust import workspace_trust

            root = getattr(self.agent.config, "project_root", ".") or "."
            if workspace_trust.is_trusted(root):
                return
            if not workspace_trust.has_execution_surface(root):
                return
            if await self._prompt_workspace_trust(root):
                workspace_trust.record_trust(root)
                get_services().events.emit(
                    "agent_status",
                    message="Workspace trusted — project hooks/config enabled.",
                )
            else:
                get_services().events.emit(
                    "agent_warning",
                    message=(
                        "Workspace left untrusted — project hooks and .coderAI/config.json "
                        "overlay are disabled. Use /trust to enable them."
                    ),
                )
        except Exception:
            logger.debug("workspace-trust gate failed; treating as untrusted", exc_info=True)

    async def _prompt_workspace_trust(self, root: Any) -> bool:
        """Ask the user to trust *root*; return True on approval.

        Uses the UI approval channel (TUI) when present, else a console prompt.
        Returns False when there is no interactive path, keeping the default
        fail-closed.
        """
        surface = self._describe_trust_surface(root)
        ipc_server = getattr(self.agent, "ipc_server", None)
        if ipc_server is not None:
            import uuid

            try:
                approved = await ipc_server.request_tool_approval(
                    tool_id=str(uuid.uuid4()),
                    tool_name="workspace_trust",
                    arguments={"folder": str(root), "enables": surface},
                )
                return bool(approved)
            except Exception:
                logger.debug("ipc workspace-trust prompt failed", exc_info=True)
                return False

        import sys

        if not sys.stdin.isatty():
            return False
        get_services().events.emit(
            "agent_status",
            message=(f"\n⚠ Untrusted workspace\n{root}\nContains: {', '.join(surface)}"),
        )
        prompt = "Trust this workspace's project automation? (y/n) > "
        try:
            from prompt_toolkit import PromptSession

            ps: PromptSession = PromptSession()
            answer = await ps.prompt_async(prompt)
        except Exception:
            answer = await asyncio.to_thread(input, prompt)
        return answer.strip().lower() in ("y", "yes")

    @staticmethod
    def _describe_trust_surface(root: Any) -> List[str]:
        """Human-readable list of the ``.coderAI`` surface a trust decision enables."""
        from pathlib import Path

        dot = Path(str(root)) / ".coderAI"
        items: List[str] = []
        try:
            if (dot / "hooks.json").is_file():
                items.append("hooks.json (runs shell commands)")
            if (dot / "config.json").is_file():
                items.append("config.json (settings overlay)")
            if (dot / "rules").is_dir():
                items.append("rules/")
            if (dot / "skills").is_dir():
                items.append("skills/")
        except OSError:
            pass
        return items or ["project automation"]

    async def _run_iteration(self, state: TurnContext) -> Optional[Dict[str, Any]]:
        """Run a single loop iteration.

        Returns a final response dict to end the turn, or ``None`` to
        continue with the next iteration.
        """
        # Per-iteration back-off after recoverable errors so retries are
        # paced rather than burned in milliseconds. Cancellation-aware:
        # ``cancel_event.wait()`` short-circuits the sleep when the user
        # hits /cancel.
        consecutive_errors = max(state.consecutive_llm_errors, state.consecutive_tool_errors)
        delay = compute_iteration_backoff(consecutive_errors)
        if delay > 0:
            cancel_event = (
                self.agent.tracker_info._cancel_event if self.agent.tracker_info else None
            )
            get_services().events.emit(
                "agent_status",
                message=(
                    f"Backing off {delay:.1f}s after {consecutive_errors} consecutive error(s)…"
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
            response_data = await self._handle_llm_phase(state)
            state.consecutive_llm_errors = 0

            outcome = await self._handle_finish_reason(state, response_data)
            if outcome is _RESTART_ITERATION:
                return None
            if outcome is not _PROCEED_TO_TOOLS:
                return outcome  # type: ignore[no-any-return]

            return await self._handle_tools_phase(state, response_data)
        except BudgetExceededError as e:
            # Terminal: budget is a hard stop, not a transient failure.
            return await self._handle_budget_exceeded(e)
        except Exception as e:
            logger.error(f"Error during processing: {e}", exc_info=True)
            state.consecutive_llm_errors += 1

            if state.consecutive_llm_errors >= MAX_CONSECUTIVE_ERRORS:
                return await self._handle_fatal_error(e, state.consecutive_llm_errors)

            state.messages = await self._handle_recoverable_error(
                e, state.consecutive_llm_errors, state.user_message
            )
            return None

    async def _handle_llm_phase(self, state: TurnContext) -> Dict[str, Any]:
        """Call the LLM (including the one-shot ``length`` retry) and return
        the parsed response data."""
        info = self.agent.tracker_info
        if info and info.status != AgentStatus.THINKING:
            self.agent.tracker_update(status=AgentStatus.THINKING)

        # Inject step reminders (plan mode, step-limit warnings)
        step_aware_messages = self._inject_step_reminders(
            state.messages, state.iteration, state.max_iterations
        )

        # Periodic MCP server health check (every 10 iterations). Launched in
        # the background so the SSE probes (5s timeout each) and reconnect
        # back-off sleeps never stall the agent's reasoning loop.
        self._health_check_counter += 1
        if self._health_check_counter >= 10:
            self._health_check_counter = 0
            self._maybe_start_mcp_health_check()

        # A completed background health check may have reconnected or dropped
        # servers; rebuild the schemas on this thread before the next LLM call.
        if self._tool_schemas_dirty:
            self._tool_schemas_dirty = False
            state.tool_schemas = self._get_tool_schemas()

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

    async def _handle_finish_reason(self, state: TurnContext, response_data: Dict[str, Any]) -> Any:
        """Persist the assistant reply and route on ``finish_reason``.

        Returns ``_PROCEED_TO_TOOLS`` to continue into the tool phase,
        ``_RESTART_ITERATION`` to restart the loop without consuming an
        iteration, or a final response dict that ends the turn.
        """
        content = response_data.get("content")
        tool_calls = response_data.get("tool_calls")
        finish_reason = response_data.get("finish_reason")

        if finish_reason == "cancelled":
            return await self._handle_cancellation()

        if content and content.strip():
            state.reply_parts.append(content.strip())

        reasoning_content = response_data.get("reasoning_content")
        session_content = content if content and str(content).strip() else None
        self.agent.session.add_message(
            "assistant",
            session_content,
            tool_calls=tool_calls,
            reasoning_content=reasoning_content,
        )
        self._refresh_messages_from_session(state.messages)

        if finish_reason == "refusal":
            get_services().events.emit(
                "agent_warning",
                message="Model refused this request (stop_reason=refusal). Returning model text without further tool calls.",
            )
            # Return the refusal content as final response — do NOT loop
            return await self._finalize_turn(
                fallback=content or "",
                stop_reason="refusal",
                iterations=state.iteration,
                hooks_data=state.hooks_data,
            )
        elif finish_reason == "length":
            # Model hit max_tokens and was cut off mid-response.
            get_services().events.emit(
                "agent_warning",
                message=(
                    "Response was truncated (max_tokens limit reached). "
                    "Increase max_tokens in config to fix this."
                ),
            )
            note = (
                "[Output cut off — the model hit the max_tokens limit. "
                "Run `coderAI config set max_tokens 16000` to increase it.]"
            )
            return await self._finalize_turn(
                tail=note,
                error=True,
                stop_reason="length",
                iterations=state.iteration,
                hooks_data=state.hooks_data,
            )
        elif finish_reason == "pause_turn":
            state.consecutive_pauses += 1
            if state.consecutive_pauses > MAX_CONSECUTIVE_PAUSES:
                get_services().events.emit(
                    "agent_warning",
                    message=(
                        f"Model returned pause_turn {state.consecutive_pauses} times in a row; "
                        "aborting to avoid an infinite loop."
                    ),
                )
                return await self._handle_max_iterations()
            preserves_tool_calls = getattr(
                self.agent.provider, "preserves_tool_calls_on_pause", False
            )
            if tool_calls and not preserves_tool_calls:
                if self.agent.session and self.agent.session.messages:
                    last = self.agent.session.messages[-1]
                    if last.role == "assistant" and last.tool_calls:
                        last.tool_calls = None
                        self._refresh_messages_from_session(state.messages)
            get_services().events.emit(
                "agent_paused",
                message="Model requested pause_turn; resuming automatically.",
            )
            state.iteration -= 1
            return _RESTART_ITERATION
        else:
            state.consecutive_pauses = 0

        return _PROCEED_TO_TOOLS

    async def _handle_tools_phase(
        self, state: TurnContext, response_data: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Execute the tool calls and post-process the results.

        Returns a final response dict to end the turn, or ``None`` to
        continue with the next iteration.
        """
        content = response_data.get("content")
        tool_calls = response_data.get("tool_calls")

        if not tool_calls:
            if state.tools_were_used and not (content or "").strip() and not state.reply_parts:
                try:
                    summary = await self._post_tool_closing_message(state.user_message)
                except BudgetExceededError:
                    return await self._handle_budget_exceeded(
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
                    state.reply_parts.append(summary.strip())

            return await self._finalize_turn(
                fallback=content or "",
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
            state.consecutive_tool_errors = 0
        elif outcome.status is BatchStatus.RETRY:
            # All tool calls failed (or were unparsable); the executor updated
            # the transcript with error feedback for the next LLM round.
            state.consecutive_tool_errors += 1
            if state.consecutive_tool_errors >= MAX_CONSECUTIVE_ERRORS:
                return await self._handle_fatal_error(
                    RuntimeError("Tool execution failed repeatedly."),
                    state.consecutive_tool_errors,
                )
        else:  # BatchStatus.OK
            state.tools_were_used = True
            state.consecutive_tool_errors = 0

        # Check budget after expensive tool operations (MCP, sub-agents,
        # summarization) that consume tokens through internal LLM calls.
        if self.agent.config.budget_limit > 0:
            check_budget_limit(
                self.agent.config.budget_limit,
                self.agent.cost_tracker,
                emit_warning=True,
            )

        # Manage context window after tool results (or error messages) are added
        state.messages = self.agent.context_controller.inject_context(
            state.messages, query=state.user_message
        )
        state.messages = await self.agent.context_controller.manage_context_window(state.messages)
        return None

    def _maybe_start_mcp_health_check(self) -> None:
        """Run the MCP health check + auto-reconnect off the critical path.

        ``check_server_health`` makes per-server network probes (5s timeout
        each) and ``auto_reconnect_degraded`` sleeps through an exponential
        back-off — both would otherwise stall the agent's LLM loop for
        seconds. We run them as a detached task and only signal the loop to
        rebuild tool schemas once the work finishes. At most one health task
        runs at a time.
        """
        task = self._mcp_health_task
        if task is not None and not task.done():
            return

        async def _run_health_check() -> None:
            try:
                mcp_client = get_services().mcp_client

                await mcp_client.check_server_health()
                await mcp_client.auto_reconnect_degraded()
                # Defer the schema rebuild to the loop thread (it owns
                # ``state.tool_schemas``); just flag that it's stale.
                self._tool_schemas_dirty = True
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("MCP health check failed: %s", e)

        old = self._mcp_health_task
        if old is not None and not old.done():
            old.cancel()
        self._mcp_health_task = asyncio.create_task(_run_health_check())

    async def _autoconnect_mcp_servers(self):
        """Auto-connect configured MCP servers from ~/.coderAI/mcp_servers.json."""
        from coderAI.tools.mcp import load_mcp_servers

        try:
            servers = load_mcp_servers().get("mcpServers", {})
            if not servers:
                return
            mcp_client = get_services().mcp_client

            for name, config in servers.items():
                if name in mcp_client.servers:
                    continue  # Already connected
                if config.get("disabled"):
                    continue  # Toggled off via /mcp — don't auto-reconnect
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
                elif transport == "http":
                    url = config.get("url")
                    if url:
                        logger.info("Auto-connecting MCP server %s via HTTP...", name)
                        res = await mcp_client.connect_http(name, url, config.get("headers"))
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
            get_services().events.emit("agent_error", message=msg)
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
        if session:
            self._repair_unpaired_tool_calls()
            self._last_repaired_msg_count = len(session.messages)
        messages = self.agent.session.get_messages_for_api()
        messages = self.agent.context_controller.inject_context(messages, query=user_message)
        return await self.agent.context_controller.manage_context_window(messages)  # type: ignore[no-any-return]

    def _repair_unpaired_tool_calls(self) -> None:
        """Ensure assistant tool calls are followed by matching tool results.

        If a previous iteration crashed after writing an assistant message with
        ``tool_calls`` but before tool result messages were appended, some
        providers reject the next request. We synthesize tool-error messages for
        any missing tool IDs so the transcript remains valid and recoverable.

        Uses a two-pass O(n) algorithm: first collect expected and seen IDs,
        then rebuild messages with synthetic injections where needed.
        """
        session = self.agent.session
        if not session or not session.messages:
            return

        msgs = session.messages

        # Pass 1: collect expected tool_call_ids per assistant index and
        # track which tool_call_ids already have corresponding tool messages.
        expected_by_assistant: Dict[int, Set[str]] = {}
        seen_tool_ids: Set[str] = set()
        for i, msg in enumerate(msgs):
            if msg.role == "assistant" and msg.tool_calls:
                ids = set()
                for tc in msg.tool_calls:
                    tc_id = (tc or {}).get("id")
                    if isinstance(tc_id, str) and tc_id:
                        ids.add(tc_id)
                if ids:
                    expected_by_assistant[i] = ids
            elif msg.role == "tool" and msg.tool_call_id:
                seen_tool_ids.add(msg.tool_call_id)

        if not expected_by_assistant:
            return

        # Count total missing tool_call_ids.
        missing_total = sum(
            len(tool_ids - seen_tool_ids) for tool_ids in expected_by_assistant.values()
        )
        if not missing_total:
            return

        # Pass 2: rebuild messages list, injecting synthetic tool responses
        # after each assistant message that has unpaired tool calls.
        repaired: List[Message] = []
        for i, msg in enumerate(msgs):
            repaired.append(msg)
            if i in expected_by_assistant:
                missing = expected_by_assistant[i] - seen_tool_ids
                if missing:
                    anchor_ts = getattr(msg, "timestamp", None) or _time.time()
                    for offset, tcid in enumerate(sorted(missing), start=1):
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

        session.messages = repaired
        session.updated_at = _time.time()
        logger.warning(
            "Recovered %s unpaired assistant tool_call(s) by injecting synthetic tool responses.",
            missing_total,
        )

    def _get_tool_schemas(self) -> Optional[List[Dict[str, Any]]]:
        """Collect tool schemas from built-in registry and MCP."""
        tool_schemas = (
            self.agent.tools.get_schemas() if self.agent.provider.supports_tools() else None
        )
        try:
            mcp_client = get_services().mcp_client

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
        messages = self.agent.context_controller.inject_context(messages, query=user_message)
        messages = await self.agent.context_controller.manage_context_window(messages)
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
    ) -> Dict[str, Any]:
        """Single terminal-turn path shared by every loop-exit site.

        Owns the previously-duplicated end-of-turn sequence: finish the tracker,
        persist the session, fire the ``on_stop`` hooks, and build the
        ``{"content", "messages", "model_info"}`` response dict.

        Content is assembled from the accumulated assistant reply parts:
        * ``content_override`` (when given) replaces the reply entirely — used by
          the fixed-message exits (fatal error / budget / max-iterations).
        * otherwise the joined reply is returned, with ``tail`` appended (after a
          blank line) when present; when the reply is empty the content falls
          back to ``tail`` if given, else ``fallback``.

        on_stop now fires on EVERY terminal path (unless ``run_stop_hooks`` is
        False) with a uniform ``{"iterations", "error": stop_reason}`` payload —
        this fixes the prior drift where length/doom/budget exits skipped it.
        ``hooks_data`` is used when supplied, else loaded fresh.
        """
        self.agent._finish_tracker(error=error)
        self.agent.save_session()

        if run_stop_hooks:
            data = hooks_data if hooks_data is not None else self.hooks_manager.load_hooks()
            if data:
                await self.hooks_manager.run_hooks(
                    "*", "on_stop", {"iterations": iterations, "error": stop_reason}, data
                )

        if content_override is not None:
            content = content_override
        else:
            joined = "\n\n".join(self._turn.reply_parts)
            if joined:
                content = f"{joined}\n\n{tail}" if tail else joined
            else:
                content = tail if tail is not None else fallback

        session = self.agent.session
        return {
            "content": content,
            "messages": session.messages if session else [],
            "model_info": self.agent.provider.get_model_info(),
        }

    async def _handle_cancellation(self) -> Dict[str, Any]:
        # Cancellation is handled consistently with the other terminal paths
        # (refusal, normal stop, max_iterations) via the shared finalizer.
        return await self._finalize_turn(
            tail="Agent stopped by user.",
            stop_reason="cancelled",
            iterations=0,
        )

    async def _handle_fatal_error(self, e: Exception, count: int) -> Dict[str, Any]:
        get_services().events.emit(
            "agent_error", message=f"Too many consecutive errors ({count}). Last: {e}"
        )
        return await self._finalize_turn(
            content_override=(
                f"I encountered {count} consecutive errors. Last error: {e}. Please try again."
            ),
            error=True,
            stop_reason="error",
        )

    async def _handle_recoverable_error(
        self, e: Exception, count: int, user_message: str
    ) -> List[Dict[str, Any]]:
        # Sanitize error message to avoid leaking sensitive info (API keys, tracebacks)
        error_str = str(e)
        # Truncate long error messages
        if len(error_str) > 200:
            error_str = error_str[:200] + "..."
        error_str = sanitize_for_log(error_str)

        get_services().events.emit(
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
        messages = self.agent.context_controller.inject_context(messages, query=user_message)
        return await self.agent.context_controller.manage_context_window(messages)  # type: ignore[no-any-return]

    async def _handle_budget_exceeded(self, e: BudgetExceededError) -> Dict[str, Any]:
        """Stop the loop cleanly when the budget has been exhausted."""
        get_services().events.emit("agent_error", message=str(e))
        return await self._finalize_turn(
            content_override=f"Blocked: {e}",
            error=True,
            stop_reason="budget",
        )

    async def _handle_max_iterations(self) -> Dict[str, Any]:
        """Handle hitting the iteration limit."""
        msg = "I've reached the maximum number of iterations. Please try again."
        get_services().events.emit("agent_warning", message=msg)
        return await self._finalize_turn(
            content_override=msg,
            error=True,
            stop_reason="max_iterations",
            iterations=self.agent.config.max_iterations,
        )

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
                    model_for_cost = getattr(self.agent.provider, "actual_model", self.agent.model)
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
                get_services().events.emit(
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
