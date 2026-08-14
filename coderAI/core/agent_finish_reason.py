"""Finish-reason routing and the single terminal-turn path."""

import logging
from typing import TYPE_CHECKING, Any, Optional, cast

from coderAI.core.agent_loop_outcomes import PROCEED_TO_TOOLS, RESTART_ITERATION
from coderAI.core.ports import AgentRuntime, RuntimeView
from coderAI.core.services import get_services
from coderAI.core.turn import TurnContext
from coderAI.system.error_policy import BudgetExceededError, MAX_CONSECUTIVE_PAUSES

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from coderAI.system.history import Session
    from coderAI.system.hooks_manager import HooksManager


class FinishReasonHandler:
    """Typed mixin contract for terminal loop outcomes."""

    agent: AgentRuntime
    runtime: RuntimeView
    hooks_manager: HooksManager
    _turn: TurnContext

    if TYPE_CHECKING:

        def _session(self) -> Session: ...
        def _refresh_messages_from_session(self, messages: list[dict[str, Any]]) -> None: ...
        def _repair_unpaired_tool_calls(self) -> None: ...

    async def _handle_finish_reason(
        self, state: TurnContext, response_data: dict[str, Any]
    ) -> object | dict[str, Any]:
        """Persist the assistant reply and route on ``finish_reason``.

        Returns ``PROCEED_TO_TOOLS`` to continue into the tool phase,
        ``RESTART_ITERATION`` to restart the loop without consuming an
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
        self._session().add_message(
            "assistant",
            cast(str, session_content),
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
                return await self._handle_pause_storm(state.consecutive_pauses, state.iteration)
            preserves_tool_calls = self.agent.provider.preserves_tool_calls_on_pause
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
            return RESTART_ITERATION
        elif finish_reason in ("stop", "tool_calls", None, ""):
            state.consecutive_pauses = 0
            return PROCEED_TO_TOOLS
        else:
            # Unknown finish_reason (e.g. content_filter, function_call legacy) — do not
            # silently enter tool phase. Treat as terminal with the raw reason.
            logger.warning("Unknown finish_reason=%r, treating as stop", finish_reason)
            get_services().events.emit(
                "agent_warning",
                message=f"Unknown finish_reason '{finish_reason}' — ending turn.",
            )
            return await self._finalize_turn(
                fallback=content or "",
                stop_reason=str(finish_reason) if finish_reason else "stop",
                iterations=state.iteration,
                hooks_data=state.hooks_data,
            )

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
    ) -> dict[str, Any]:
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

        Unpaired assistant ``tool_calls`` are repaired before persisting on
        abortive exits (refusal / length / doom / pause-storm / …) so those
        paths cannot leave a provider-rejecting transcript. A normal
        ``stop`` skips repair: Anthropic ``pause_turn`` may intentionally leave
        client ``tool_use`` blocks unpaired until the next model turn.
        """
        if repair_unpaired is None:
            repair_unpaired = stop_reason != "stop"
        if repair_unpaired:
            self._repair_unpaired_tool_calls()
        self.agent._finish_tracker(error=error)
        if self._turn.objective_state is not None:
            self._turn.objective_state.persist()
        self.agent.save_session()

        if run_stop_hooks:
            # An execution-time plan amendment restores the read-only boundary
            # mid-turn. Do not let an already-loaded project hook execute after
            # that transition.
            data = (
                {}
                if self.runtime.plan_mode
                else hooks_data
                if hooks_data is not None
                else self.hooks_manager.load_hooks()
            )
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
        success = not error and stop_reason == "stop"
        return {
            "content": content,
            "messages": session.messages if session else [],
            "model_info": self.agent.provider.get_model_info(),
            "success": success,
            "stop_reason": stop_reason,
            "error": None if success else stop_reason,
            "objective_state": self._turn.objective_state.as_dict()
            if self._turn.objective_state is not None
            else None,
        }

    async def _handle_cancellation(self) -> dict[str, Any]:
        # Cancellation is handled consistently with the other terminal paths
        # (refusal, normal stop, max_iterations) via the shared finalizer.
        return await self._finalize_turn(
            tail="Agent stopped by user.",
            stop_reason="cancelled",
            iterations=0,
        )

    async def _handle_fatal_error(self, e: Exception, count: int) -> dict[str, Any]:
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

    async def _handle_budget_exceeded(self, e: BudgetExceededError) -> dict[str, Any]:
        """Stop the loop cleanly when the budget has been exhausted."""
        get_services().events.emit("agent_error", message=str(e))
        return await self._finalize_turn(
            content_override=f"Blocked: {e}",
            error=True,
            stop_reason="budget",
        )

    async def _handle_max_iterations(self) -> dict[str, Any]:
        """Handle hitting the iteration limit."""
        msg = "I've reached the maximum number of iterations. Please try again."
        get_services().events.emit("agent_warning", message=msg)
        return await self._finalize_turn(
            content_override=msg,
            error=True,
            stop_reason="max_iterations",
            iterations=self.agent.config.max_iterations,
        )

    async def _handle_pause_storm(self, pause_count: int, iterations: int) -> dict[str, Any]:
        """Abort when the model returns ``pause_turn`` too many times in a row."""
        msg = (
            f"Model returned pause_turn {pause_count} times in a row; "
            "aborting to avoid an infinite loop."
        )
        get_services().events.emit("agent_warning", message=msg)
        return await self._finalize_turn(
            content_override=msg,
            error=True,
            stop_reason="pause_storm",
            iterations=iterations,
        )
