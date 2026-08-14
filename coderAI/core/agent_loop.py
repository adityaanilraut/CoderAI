"""Execution Loop orchestrator for CoderAI agent."""

import asyncio
import logging
import time as _time
from pathlib import Path
from typing import Any, Optional

from coderAI.core.agent_tracker import AgentStatus
from coderAI.core.objective import ObjectiveState
from coderAI.system.cost import CostTracker
from coderAI.system.history import Session
from coderAI.core.services import get_services
from coderAI.core.loop_guard import (
    IN_BATCH_DOOM_THRESHOLD,
    LoopGuard,
)
from coderAI.core.tool_executor import ToolExecutor
from coderAI.core.turn import TurnContext
from coderAI.core.ports import AgentRuntime, ProgressCallback, RuntimeView, await_approval
from coderAI.core.agent_finish_reason import FinishReasonHandler
from coderAI.core.agent_llm_phase import LLMPhase
from coderAI.core.agent_recovery import RecoveryHandler
from coderAI.core.agent_tools_phase import ToolsPhase
from coderAI.core.agent_loop_outcomes import (
    CANCELLED_REQUEST as _CANCELLED_REQUEST,  # noqa: F401 - compatibility export
    PROCEED_TO_TOOLS as _PROCEED_TO_TOOLS,
    RECOVERABLE_ERROR_MARKER as RECOVERABLE_ERROR_MARKER,
    RESTART_ITERATION as _RESTART_ITERATION,
)
from coderAI.system.error_policy import (
    BudgetExceededError,
    compute_iteration_backoff,
    MAX_CONSECUTIVE_ERRORS,
)

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
# Re-exported from agent_loop_outcomes for backwards compatibility.

# Sentinels returned by ``ExecutionLoop._handle_finish_reason`` to steer the
# iteration: continue into the tool phase, or restart the loop without
# consuming an iteration (pause_turn).
# ``_CANCELLED_REQUEST`` remains exported as the historical request-cancel
# sentinel even though the LLM phase now owns its use.


class ExecutionLoop(LLMPhase, ToolsPhase, FinishReasonHandler, RecoveryHandler):
    """Manages the main LLM-Tool interaction loop."""

    def __init__(
        self,
        agent: AgentRuntime,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> None:
        self.agent = agent
        self.runtime = RuntimeView(agent)
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
        self.hooks_manager = agent.hooks_manager
        # Turn-scoped flags. ``run()`` resets these at the top of each call so
        # state never leaks across user messages.
        self._length_retry_used: bool = False
        self._hard_cap_warned: bool = False
        # MCP health-check counter, dirty flag, and background task live on
        # ``Agent`` so they survive the per-message ``ExecutionLoop`` instance.

    def _inject_step_reminders(
        self,
        messages: list[dict[str, Any]],
        iteration: int,
        max_iterations: int,
    ) -> list[dict[str, Any]]:
        """Append a ``system`` reminder when the iteration budget is nearly spent.

        The step-limit hint is emitted on every iteration that falls inside the
        5-step window so the model can see the budget shrink in real time.
        """
        result = list(messages)
        parts: list[str] = []

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
            result.append(
                {
                    "role": "system",
                    "content": combined,
                    self.agent.context_controller._EPHEMERAL_MARKER_KEY: True,
                }
            )
        return result

    def _refresh_messages_from_session(self, messages: list[dict[str, Any]]) -> None:
        """Replace the in-memory message list with the session transcript."""
        if self.agent.session is None:
            return
        messages.clear()
        messages.extend(self.agent.session.get_messages_for_api())

    def _session(self) -> Session:
        """Return the session established by ``_prepare_session``."""
        session = self.agent.session
        if session is None:
            raise RuntimeError("ExecutionLoop requires an initialized session")
        return session

    async def run(self, user_message: str) -> dict[str, Any]:
        """Process a user message and return response."""

        objective_started_at = _time.monotonic()
        # Reset turn-scoped flags so state never leaks across user messages.
        self._length_retry_used = False
        self._hard_cap_warned = False

        # 1. Prepare session and check budget
        budget_block = self._prepare_session(user_message)
        if budget_block:
            return budget_block

        if self.runtime.read_cache is not None:
            self.runtime.read_cache.bump_turn()

        # 1b. First-run workspace-trust gate — decide trust before any hook or
        # project-config surface is honoured this turn.
        plan_mode = self.runtime.plan_mode
        if not plan_mode:
            await self._ensure_workspace_trust()

        # Auto-connect MCP servers only after the workspace trust decision. A
        # bundled or user-configured launcher must never run before an untrusted
        # checkout has been assessed.
        if not self.agent._mcp_initialized and not plan_mode:
            self.agent._mcp_initialized = True
            await self._autoconnect_mcp_servers()

        # 2. Run on_user_prompt and chat.message hooks
        # Project hooks may execute shell commands. Plan Mode is an enforced
        # read-only boundary, so suppress every hook phase by carrying an empty
        # (non-None) hook set through the turn; the finalizer must not reload it.
        hooks_data = {} if plan_mode else self.hooks_manager.load_hooks()
        if hooks_data:
            await self.hooks_manager.run_hooks(
                "*", "on_user_prompt", {"text": user_message}, hooks_data
            )

            transformed = await self.hooks_manager.run_chat_message_hooks(user_message, hooks_data)
            if transformed:
                user_message = transformed

        # 3. Persist the user message so the LLM sees what was asked
        self._session().add_message("user", user_message)

        # 4. Prepare messages (retrieve, inject context, manage window)
        messages = await self._prepare_messages(user_message)

        tool_schemas = self._get_tool_schemas(user_message)

        # Process with LLM (potentially multiple rounds for tool calls)
        max_iterations = self.agent.config.max_iterations
        hard_cap = self.agent.config.max_iterations_hard_cap
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
        objective_state = ObjectiveState(
            objective=user_message,
            plan_id=self.runtime.active_plan_id,
            plan_revision=self.runtime.active_plan_revision,
        )
        run_context = self.runtime.run_context
        objective_store = run_context.objective_store if run_context is not None else None
        if objective_store is not None:
            objective_state.bind_persistence(
                lambda current: objective_store.save(current, run_context=run_context)
            )
        self.agent.last_objective_state = objective_state
        state = TurnContext(
            user_message=user_message,
            messages=messages,
            tool_schemas=tool_schemas,
            hooks_data=hooks_data,
            max_iterations=max_iterations,
            objective_state=objective_state,
            objective_started_at=objective_started_at,
            routed_tool_names={
                str((schema.get("function") or {}).get("name"))
                for schema in tool_schemas or []
                if (schema.get("function") or {}).get("name")
            },
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
        workspace untrusted for this Agent. Approval records trust for the next
        launch; no project-controlled surface activates mid-session.
        """
        if self.agent._workspace_trust_checked:
            return
        self.agent._workspace_trust_checked = True
        try:
            from coderAI.system.trust import workspace_trust

            root = self.agent.config.project_root or "."
            if self.agent._workspace_trusted:
                return
            if not workspace_trust.has_execution_surface(root):
                return
            if await self._prompt_workspace_trust(root):
                try:
                    workspace_trust.record_trust(root)
                except (OSError, ValueError) as e:
                    get_services().events.emit(
                        "agent_warning",
                        message=f"Workspace trust was not recorded: {e}",
                    )
                    return
                get_services().events.emit(
                    "agent_status",
                    message=(
                        "Workspace trust recorded. Restart CoderAI to enable project "
                        "config, hooks, rules, skills, and personas; they remain disabled "
                        "for this session."
                    ),
                )
            else:
                get_services().events.emit(
                    "agent_warning",
                    message=(
                        "Workspace left untrusted — project config, hooks, rules, skills, "
                        "and personas are disabled. Use /trust, then restart CoderAI, to "
                        "enable them."
                    ),
                )
        except Exception:
            logger.debug("workspace-trust gate failed; treating as untrusted", exc_info=True)

    async def _prompt_workspace_trust(self, root: str | Path) -> bool:
        """Ask the user to trust *root*; return True on approval.

        Uses the host :class:`~coderAI.core.ports.ApprovalPort` when present,
        else a console prompt. Returns False when there is no interactive path,
        keeping the default fail-closed.
        """
        surface = self._describe_trust_surface(root)
        port = self.runtime.approval_port
        if port is not None:
            try:
                return await await_approval(
                    port,
                    "workspace_trust",
                    {"folder": str(root), "enables": surface},
                )
            except Exception:
                logger.debug("approval-port workspace-trust prompt failed", exc_info=True)
                return False

        import sys

        if not sys.stdin.isatty():
            return False
        get_services().events.emit(
            "agent_status",
            message=(f"\n⚠ Untrusted workspace\n{root}\nContains: {', '.join(surface)}"),
        )
        prompt = "Trust this workspace's project automation on next launch? (y/n) > "
        try:
            from prompt_toolkit import PromptSession

            ps: PromptSession = PromptSession()
            answer = await ps.prompt_async(prompt)
        except Exception:
            answer = await asyncio.to_thread(input, prompt)
        return answer.strip().lower() in ("y", "yes")

    @staticmethod
    def _describe_trust_surface(root: str | Path) -> list[str]:
        """Human-readable list of the ``.coderAI`` surface a trust decision enables."""
        from pathlib import Path

        dot = Path(str(root)) / ".coderAI"
        items: list[str] = []
        try:
            if (dot / "hooks.json").is_file():
                items.append("hooks.json (runs shell commands)")
            if (dot / "config.json").is_file():
                items.append("config.json (settings overlay)")
            if (dot / "rules").is_dir():
                items.append("rules/")
            if (dot / "skills").is_dir():
                items.append("skills/")
            if (dot / "agents").is_dir():
                items.append("agents/")
        except OSError:
            pass
        return items or ["project automation"]

    async def _run_iteration(self, state: TurnContext) -> Optional[dict[str, Any]]:
        """Run a single loop iteration.

        Returns a final response dict to end the turn, or ``None`` to
        continue with the next iteration.
        """
        # High-impact fix: iteration-level backoff is now cancellation-aware
        # with jitter and capped at 2s for fast recovery. The heavy retry
        # backoff lives in _call_llm_with_retry where it is header-aware.
        # This keeps the loop responsive to /cancel even during retry storms.
        consecutive_errors = max(state.consecutive_llm_errors, state.consecutive_tool_errors)
        if consecutive_errors > 0:
            delay = min(compute_iteration_backoff(consecutive_errors), 2.0)
            if delay > 0.1:
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
                        return await self._handle_cancellation()
                    except asyncio.TimeoutError:
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
            if outcome is _PROCEED_TO_TOOLS:
                return await self._handle_tools_phase(state, response_data)
            if isinstance(outcome, dict):
                return outcome
            raise RuntimeError("finish-reason handler returned an unknown loop outcome")
        except BudgetExceededError as e:
            # Terminal: budget is a hard stop, not a transient failure.
            return await self._handle_budget_exceeded(e)
        except (TypeError, AttributeError, AssertionError, ImportError, NotImplementedError) as e:
            # Programming errors — fail fast, do NOT feed synthetic recovery to the model.
            logger.error(f"Fatal programming error during iteration: {e}", exc_info=True)
            return await self._handle_fatal_error(e, MAX_CONSECUTIVE_ERRORS)
        except (RuntimeError, ValueError, OSError, KeyError, IndexError) as e:
            logger.error(f"Recoverable error during processing: {e}", exc_info=True)
            state.consecutive_llm_errors += 1
            if state.consecutive_llm_errors >= MAX_CONSECUTIVE_ERRORS:
                return await self._handle_fatal_error(e, state.consecutive_llm_errors)
            state.messages = await self._handle_recoverable_error(
                e, state.consecutive_llm_errors, state.user_message
            )
            return None
        except Exception as e:
            # Fallback for any other Exception subclasses — treat as recoverable
            # but log at warning to distinguish from the narrower handlers above.
            logger.warning(
                f"Unhandled exception type {type(e).__name__} during processing: {e}", exc_info=True
            )
            state.consecutive_llm_errors += 1
            if state.consecutive_llm_errors >= MAX_CONSECUTIVE_ERRORS:
                return await self._handle_fatal_error(e, state.consecutive_llm_errors)
            state.messages = await self._handle_recoverable_error(
                e, state.consecutive_llm_errors, state.user_message
            )
            return None

    async def _autoconnect_mcp_servers(self) -> None:
        """Auto-connect configured + bundled MCP servers (e.g. git_extended)."""
        from coderAI.tools.mcp import effective_mcp_servers
        from coderAI.tools.mcp_config import connect_from_entry

        try:
            trusted = self.runtime.workspace_trusted
            root = self.agent.config.project_root or "."
            mcp_client = get_services().mcp_client
            mcp_client.set_project_root(root)
            servers = effective_mcp_servers(project_root=root, workspace_trusted=trusted).get(
                "mcpServers", {}
            )
            if not servers:
                return

            for name, config in servers.items():
                if name in mcp_client.servers:
                    continue  # Already connected
                if config.get("disabled") or config.get("_connect_blocked"):
                    continue  # Toggled off / pending project approval
                logger.info(
                    "Auto-connecting MCP server %s via %s...",
                    name,
                    config.get("transport", "stdio"),
                )
                res = await connect_from_entry(
                    name,
                    config,
                    project_root=root,
                    client=mcp_client,
                )
                if not res.get("success"):
                    logger.error("Failed to auto-connect MCP server %s: %s", name, res.get("error"))
        except Exception as e:
            logger.error("Error auto-connecting MCP servers: %s", e)

    def _prepare_session(self, user_message: str) -> Optional[dict[str, Any]]:
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
                "success": False,
                "stop_reason": "budget",
                "error": msg,
            }
        return None

    async def _prepare_messages(self, user_message: str) -> list[dict[str, Any]]:
        """Retrieve messages from session and inject pinned context."""
        session = self.agent.session
        if session:
            self._repair_unpaired_tool_calls()
        messages = self._session().get_messages_for_api()
        for content in self.runtime.active_skill_context:
            messages.append(
                {
                    "role": "system",
                    "content": content,
                    self.agent.context_controller._EPHEMERAL_MARKER_KEY: True,
                }
            )
        messages = self.agent.context_controller.inject_context(messages, query=user_message)
        return messages

    def _get_tool_schemas(
        self,
        objective: str = "",
        *,
        warm_tool_names: Optional[set[str]] = None,
    ) -> Optional[list[dict[str, Any]]]:
        """Route eligible native and MCP schemas for one objective."""
        from coderAI.core.capability_routing import route_capabilities

        native_schemas: list[dict[str, Any]] = []
        plan_mode = self.runtime.plan_mode
        supports_tools = bool(self.agent.provider.supports_tools())
        if supports_tools:
            if plan_mode:
                selected: list[dict[str, Any]] = []
                for tool in self.agent.tools.get_all():
                    if (
                        tool.name == "submit_plan"
                        or tool.is_read_only
                        or tool.name == "delegate_task"
                    ):
                        selected.append(tool.get_schema())
                native_schemas = selected
            else:
                native_schemas = [
                    schema
                    for schema in self.agent.tools.get_schemas()
                    if (schema.get("function") or {}).get("name") != "submit_plan"
                    and (
                        (schema.get("function") or {}).get("name") != "request_plan_amendment"
                        or bool(self.runtime.active_plan_id)
                    )
                ]
        mcp_schemas: list[dict[str, Any]] = []
        try:
            mcp_client = get_services().mcp_client

            mcp_schemas = [] if plan_mode else mcp_client.get_tools_as_openai_format()
            # Domain-scoped sub-agents fail closed on dynamic MCP. Server-side
            # annotations are untrusted, and no local exact-tool trust metadata
            # mechanism exists yet.
            if not self.runtime.allow_dynamic_mcp:
                mcp_schemas = []
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
        except Exception as e:
            logger.debug(f"MCP tool discovery skipped: {e}")

        if not supports_tools:
            decision_schemas: list[dict[str, Any]] = []
            selected_names: list[str] = []
            routing_reason = "provider_without_tools"
            selection_success = False
        else:
            decision = route_capabilities(
                objective=objective,
                native_schemas=native_schemas,
                mcp_schemas=mcp_schemas,
                warm_tool_names=warm_tool_names or set(),
                plan_mode=plan_mode,
                active_plan=bool(self.runtime.active_plan_id),
            )
            decision_schemas = list(decision.schemas)
            selected_names = list(decision.selected_names)
            routing_reason = decision.routing_reason
            selection_success = decision.selection_success

        try:
            schema_token_cost = self.agent.context_controller.estimate_tool_tokens(decision_schemas)
            if not isinstance(schema_token_cost, int):
                schema_token_cost = 0
        except Exception:
            schema_token_cost = 0
        try:
            get_services().events.emit(
                "capability_routing",
                schema_token_cost=schema_token_cost,
                selected_capabilities=selected_names,
                routing_reason=routing_reason,
                selection_success=selection_success,
                plan_mode=plan_mode,
            )
        except Exception:
            logger.debug("Capability routing event emission skipped", exc_info=True)
        return decision_schemas or None
