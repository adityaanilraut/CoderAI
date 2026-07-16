"""Agent session mixin: session lifecycle, token accounting, cost tracking, and checkpoints.

Mixed into ``Agent`` (``class Agent(AgentCapabilitiesMixin, AgentSessionMixin)``)
so these run as ordinary instance methods. Cross-calls to other agent methods go
through ``self.xxx()`` so that test stubs with mocked instance methods are
respected. The class-body annotations declare the ``Agent`` state this mixin
reads and writes; all of it is assigned in ``Agent.__init__``.
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Set, Tuple, TYPE_CHECKING

from coderAI.context.context_controller import RESPONSE_TOKEN_RESERVE, TOOL_OVERHEAD_TOKENS
from coderAI.core.agent_tracker import AgentStatus, AgentInfo
from coderAI.core.services import get_services
from coderAI.llm.base import normalize_usage
from coderAI.system.error_policy import check_budget_limit
from coderAI.system.read_cache import FileReadCache
from coderAI.system.history import (
    SESSION_SCHEMA_VERSION,
    Message,
    Session,
    checkpoint_label,
)

if TYPE_CHECKING:
    from coderAI.context.context_controller import ContextController
    from coderAI.core.personas import AgentPersona
    from coderAI.system.config import Config
    from coderAI.system.cost import CostTracker
    from coderAI.system.hooks_manager import HooksManager

logger = logging.getLogger(__name__)

_UNSET: Any = object()


class AgentSessionMixin:
    """Session lifecycle, token/cost accounting, tracker, and checkpoints."""

    # Agent state used by this mixin (assigned in ``Agent.__init__``).
    config: Config
    model: str
    persona: Optional[AgentPersona]
    provider: Any
    is_subagent: bool
    session: Optional[Session]
    cost_tracker: CostTracker
    hooks_manager: HooksManager
    read_cache: FileReadCache
    tracker_info: Optional[AgentInfo]
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    total_cache_creation_tokens: int
    total_cache_read_tokens: int
    _context_controller: ContextController
    _tracker_start_completion: int
    _tracker_start_tokens: int
    _tracker_start_cost: float
    _hooks_approved: Dict[str, bool]
    _save_executor: Optional[ThreadPoolExecutor]
    _pending_saves: Set["Future[Any]"]

    if TYPE_CHECKING:
        # Provided by AgentCapabilitiesMixin on the composed Agent class.
        def _wire_read_cache(self) -> None: ...
        def _get_system_prompt(self) -> str: ...
        def _refresh_session_system_prompt(self) -> None: ...
        def _configure_delegate_tool_context(self) -> None: ...

    def _reset_session_accounting(self, *, reset_cost: bool = True) -> None:
        """Reset cost, token, and hook-approval state at session boundaries.

        The ``Agent`` outlives individual sessions (e.g. ``/clear`` creates a
        new one, ``history`` can load another). Without this reset the
        previous session's spend would count against the new session's
        budget, and prior hook approvals would carry over silently.
        """
        if reset_cost:
            self.cost_tracker.reset()
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_tokens = 0
        self.total_cache_creation_tokens = 0
        self.total_cache_read_tokens = 0
        self._hooks_approved.clear()
        allowlist = getattr(self, "_tool_approval_allowlist", None)
        if allowlist is not None:
            allowlist.clear()
        if hasattr(self, "read_cache") and self.read_cache is not None:
            self.read_cache = FileReadCache()
            self._wire_read_cache()
        provider = getattr(self, "provider", None)
        if provider is not None:
            provider.reset_usage()

    def create_session(self, *, reset_accounting: bool = True) -> Session:
        """Create a new conversation session."""
        if reset_accounting:
            self._reset_session_accounting()
        self.session = get_services().history.create_session(model=self.model)
        self.session.add_message("system", self._get_system_prompt())
        return self.session

    def _restore_session_accounting(self, session: Session) -> None:
        """Restore persisted spend so resume cannot reset the session budget."""
        self.total_prompt_tokens = session.prompt_tokens
        self.total_completion_tokens = session.completion_tokens
        self.total_tokens = session.total_tokens or (
            session.prompt_tokens + session.completion_tokens
        )
        self.total_cache_creation_tokens = session.cache_creation_tokens
        self.total_cache_read_tokens = session.cache_read_tokens
        self.cost_tracker.total_cost_usd = session.total_cost_usd

    def _sync_session_accounting(self) -> None:
        """Copy live accounting into the persisted session snapshot."""
        if self.session is None:
            return
        self.session.schema_version = SESSION_SCHEMA_VERSION
        self.session.prompt_tokens = self.total_prompt_tokens
        self.session.completion_tokens = self.total_completion_tokens
        self.session.total_tokens = self.total_tokens
        self.session.cache_creation_tokens = self.total_cache_creation_tokens
        self.session.cache_read_tokens = self.total_cache_read_tokens
        self.session.total_cost_usd = self.cost_tracker.get_total_cost()

    def load_session(self, session_id: str) -> Optional[Session]:
        """Load an existing session."""
        self._reset_session_accounting()
        self.session = get_services().history.load_session(session_id)
        if self.session:
            self._restore_session_accounting(self.session)
            self._refresh_session_system_prompt()
        return self.session

    def save_session(self) -> None:
        """Persist the current session without blocking the agent loop.

        The session is snapshotted (``model_dump``) on the calling thread so
        the write can't race with in-loop mutations, then the blocking disk
        I/O is offloaded to a single-worker background thread when an event
        loop is running. Outside a loop (sync/CLI paths) the write runs inline.
        """
        if not (self.session and self.config.save_history):
            return
        self._sync_session_accounting()
        try:
            snapshot = self.session.model_dump()
        except Exception:
            logger.warning("save_session snapshot failed", exc_info=True)
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            get_services().history.save_session_data(snapshot)
            return
        if self._save_executor is None:
            self._save_executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="coderAI-save"
            )
        future = self._save_executor.submit(get_services().history.save_session_data, snapshot)
        self._pending_saves.add(future)

        def _on_done(f: "Future[Any]") -> None:
            self._pending_saves.discard(f)
            exc = f.exception()
            if exc is not None:
                logger.warning("Background session save failed: %s", exc)

        future.add_done_callback(_on_done)

    def _flush_pending_saves(self, timeout: float = 5.0) -> None:
        """Block until queued background session writes finish (best effort)."""
        for future in list(self._pending_saves):
            try:
                future.result(timeout=timeout)
            except Exception:
                logger.debug("pending save did not complete cleanly", exc_info=True)
        if self._save_executor is not None:
            self._save_executor.shutdown(wait=True)
            self._save_executor = None

    def get_context_usage(self) -> Tuple[int, int]:
        """Get the current context window usage and limit."""
        messages = self.session.get_messages_for_api() if self.session else []
        messages = self._context_controller.inject_context(messages)
        used_tokens = self._context_controller.estimate_tokens(messages)
        limit = self.config.context_window
        return used_tokens, limit

    def _add_summary_tokens(self, input_delta: int, output_delta: int) -> None:
        """Update cumulative token counters after a context summarization LLM call."""
        self.total_prompt_tokens += input_delta
        self.total_completion_tokens += output_delta
        self.total_tokens += input_delta + output_delta

    async def _record_auxiliary_usage(self, raw_usage: Dict[str, Any]) -> None:
        """Meter a provider call made outside the main execution loop."""
        usage = normalize_usage(raw_usage)
        input_tokens = usage["input_tokens"]
        output_tokens = usage["output_tokens"]
        self.total_prompt_tokens += input_tokens
        self.total_completion_tokens += output_tokens
        self.total_tokens += input_tokens + output_tokens
        self.total_cache_creation_tokens += usage["cache_creation_tokens"]
        self.total_cache_read_tokens += usage["cache_read_tokens"]
        if input_tokens or output_tokens:
            model = getattr(self.provider, "actual_model", self.model)
            await self.cost_tracker.add_cost(model, input_tokens, output_tokens)
            check_budget_limit(self.config.budget_limit, self.cost_tracker, emit_warning=True)

    async def compact_context(self) -> bool:
        """Force the context to be compacted by summarizing history."""
        if not self.session:
            return False
        get_services().events.emit("agent_status", message="Force compacting context...")
        compact_limit = RESPONSE_TOKEN_RESERVE + TOOL_OVERHEAD_TOKENS + 1500
        try:
            messages = self.session.get_messages_for_api()
            compacted_messages = await self._context_controller.manage_context_window(
                messages, context_limit_override=compact_limit
            )
            for compacted_msg in compacted_messages:
                content_val = compacted_msg.get("content")
                if (
                    compacted_msg.get("role") == "system"
                    and isinstance(content_val, str)
                    and (
                        "[Prior Conversation Summary]:" in content_val
                        or "were removed to fit" in content_val
                    )
                ):
                    now = _time.time()

                    def _freeze(value: Any) -> Any:
                        if isinstance(value, dict):
                            return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
                        if isinstance(value, list):
                            return tuple(_freeze(v) for v in value)
                        return value

                    originals_by_identity: Dict[tuple, List[float]] = {}
                    for orig in self.session.messages:
                        key = (
                            orig.role,
                            orig.content,
                            _freeze(orig.tool_calls),
                            orig.tool_call_id,
                            orig.name,
                        )
                        originals_by_identity.setdefault(key, []).append(orig.timestamp)
                    new_messages = []
                    for cm in compacted_messages:
                        msg_args = {
                            k: v
                            for k, v in cm.items()
                            if k in ["role", "content", "tool_calls", "tool_call_id", "name"]
                        }
                        role_val = str(msg_args.get("role") or "")
                        content_val2 = msg_args.get("content")
                        content_str = str(content_val2) if content_val2 is not None else None
                        tc_id_val = msg_args.get("tool_call_id")
                        tc_id_str = str(tc_id_val) if tc_id_val is not None else None
                        name_val = msg_args.get("name")
                        name_str = str(name_val) if name_val is not None else None
                        lookup_key = (
                            role_val,
                            content_str,
                            _freeze(msg_args.get("tool_calls")),
                            tc_id_str,
                            name_str,
                        )
                        stamps = originals_by_identity.get(lookup_key)
                        if stamps:
                            msg_args["timestamp"] = stamps.pop(0)
                        else:
                            msg_args["timestamp"] = now
                        new_messages.append(Message(**msg_args))
                    self.session.messages = new_messages
                    self.session.updated_at = now
                    self.session.checkpoints = []
                    self.save_session()
                    hooks_data = self.hooks_manager.load_hooks()
                    if hooks_data:
                        await self.hooks_manager.run_hooks("*", "on_compact", {}, hooks_data)
                    get_services().events.emit(
                        "agent_status", message="Context compacted successfully!"
                    )
                    return True
            get_services().events.emit(
                "agent_status", message="Context already compact or could not be compacted."
            )
            return False
        except Exception as e:
            logger.error(f"Error during manual context compaction: {e}")
            return False

    def _register_tracker(
        self, task: str, role: Optional[str] = None, parent_id: Optional[str] = None
    ) -> AgentInfo:
        """Register this agent with the global tracker."""
        persona = self.persona
        self.tracker_info = get_services().agent_tracker.register(
            name=persona.name if persona else "main",
            role=role or (persona.description if persona else None),
            model=self.model,
            parent_id=parent_id,
            context_limit=self.config.context_window,
        )
        self._tracker_start_completion = self.total_completion_tokens
        self._tracker_start_tokens = self.total_tokens
        self._tracker_start_cost = self.cost_tracker.get_total_cost()
        self.tracker_info.current_task = task
        self.tracker_info.status = AgentStatus.THINKING
        get_services().events.emit("agent_lifecycle", action="started", info=self.tracker_info)
        self._configure_delegate_tool_context()
        return self.tracker_info

    def tracker_update(
        self,
        *,
        status: Any = _UNSET,
        current_tool: Any = _UNSET,
        current_task: Any = _UNSET,
    ) -> None:
        """Mutate tracker fields through the Agent (Phase 4.1).

        The execution loop and tool executor used to write ``tracker_info.status``
        / ``current_tool`` directly; those field writes now funnel through here so
        tracker mutation stays owned by the Agent. No-op when there is no active
        tracker.
        """
        info = self.tracker_info
        if info is None:
            return
        if status is not _UNSET:
            info.status = status
        if current_tool is not _UNSET:
            info.current_tool = current_tool
        if current_task is not _UNSET:
            info.current_task = current_task
        self._sync_tracker()

    def _sync_tracker(self) -> None:
        """Sync internal token counters to the tracker info."""
        info = self.tracker_info
        if not info:
            return
        info.completion_tokens = self.total_completion_tokens - self._tracker_start_completion
        info.total_tokens = self.total_tokens - self._tracker_start_tokens
        info.cost_usd = self.cost_tracker.get_total_cost() - self._tracker_start_cost
        if self.session:
            msgs = self.session.get_messages_for_api()
            info.context_used_tokens = self._context_controller.estimate_tokens(msgs)
        get_services().events.emit("agent_tracker_sync", info=info)

    def _finish_tracker(self, error: bool = False) -> None:
        """Mark this agent as done in the tracker and emit completion event."""
        info = self.tracker_info
        if not info:
            return
        self._sync_tracker()
        if info.status != AgentStatus.CANCELLED:
            info.status = AgentStatus.ERROR if error else AgentStatus.DONE
        info.finished_at = _time.time()
        get_services().events.emit("agent_lifecycle", action="finished", info=info)

    def _record_checkpoint(self, user_message: str) -> None:
        """Record a per-turn rewind point on the active session.

        Skipped for sub-agents and when ``enable_checkpoints`` is off. Called
        at the top of ``process_message`` so ``message_index`` excludes this
        turn's skill injections, user message, and assistant reply.
        """
        if self.is_subagent or not self.config.enable_checkpoints:
            return
        if self.session is None:
            return
        self.session.add_checkpoint(checkpoint_label(user_message))

    def rewind_to(self, turn: int, restore_files: bool = False) -> Dict[str, Any]:
        """Rewind the conversation to before the given user ``turn``.

        Truncates message history (and the checkpoint list) back to that
        checkpoint. When ``restore_files`` is set, file edits recorded since
        the checkpoint are reverted via the per-session backup store.

        Returns a result dict: ``{"ok": True, "turn", "label", "dropped_turns",
        "restored_files", "file_errors"}`` on success, or
        ``{"ok": False, "error"}`` if the session or turn is invalid.
        """
        if self.session is None:
            return {"ok": False, "error": "No active session."}
        target = next((c for c in self.session.checkpoints if c.turn == turn), None)
        if target is None:
            valid = [c.turn for c in self.session.checkpoints]
            detail = f" Valid turns: {valid}." if valid else " No rewind points recorded yet."
            return {"ok": False, "error": f"No checkpoint for turn {turn}.{detail}"}
        dropped = sum(1 for c in self.session.checkpoints if c.turn >= turn)
        cutoff = target.created_at
        self.session.truncate_to_checkpoint(turn)
        restored_files: List[str] = []
        file_errors: List[str] = []
        if restore_files:
            from coderAI.tools.undo import get_backup_store

            result = get_backup_store().restore_after(cutoff)
            restored_files = list(result.get("restored", [])) + list(result.get("deleted", []))
            file_errors = list(result.get("errors", []))
        self.save_session()
        return {
            "ok": True,
            "turn": turn,
            "label": target.label,
            "dropped_turns": dropped,
            "restored_files": restored_files,
            "file_errors": file_errors,
        }

    def get_model_info(self) -> Dict[str, Any]:
        """Get information about the current model.

        The provider supplies static details (name, model, endpoint), but the
        token totals come from the Agent — the source of truth for the session —
        so they stay continuous across a mid-session provider/model swap.
        """
        info: Dict[str, Any] = self.provider.get_model_info()
        info["total_input_tokens"] = self.total_prompt_tokens
        info["total_output_tokens"] = self.total_completion_tokens
        info["total_tokens"] = self.total_tokens
        if "cache_creation_tokens" in info or self.total_cache_creation_tokens:
            info["cache_creation_tokens"] = self.total_cache_creation_tokens
        if "cache_read_tokens" in info or self.total_cache_read_tokens:
            info["cache_read_tokens"] = self.total_cache_read_tokens
        return info

    async def close(self) -> None:
        """Flush pending session saves and finish the tracker."""
        if self._pending_saves or self._save_executor is not None:
            self._flush_pending_saves()
        if self.tracker_info:
            self._finish_tracker()
