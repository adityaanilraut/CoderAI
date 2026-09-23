"""Session lifecycle and public APIs for the CoderAI runtime.

Persistence: append-only JSONL event log + a sessions index under
`~/.coderai/projects/<projectCode>/`, with token-threshold compaction,
isolated GitFileHistory checkpoint-based undo, subagent orchestration, and Plan Mode gating.

Event model: ``SessionEvent`` from ``coderai.events`` is the canonical
log entry.  Legacy ``SessionMessage`` is preserved for backward compat with
existing session files and CLI rendering.
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import shutil
import threading
import uuid
from typing import Any
from collections.abc import Callable

logger = logging.getLogger(__name__)

_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def _track_background_task(
    task: asyncio.Task[Any], task_name: str = "background_task"
) -> asyncio.Task[Any]:
    """Retain strong reference to fire-and-forget task and log unexpected exceptions."""
    _BACKGROUND_TASKS.add(task)

    def _on_done(t: asyncio.Task[Any]) -> None:
        _BACKGROUND_TASKS.discard(t)
        if not t.cancelled():
            exc = t.exception()
            if exc is not None:
                logger.warning("Background task %s failed with exception: %s", task_name, exc)

    task.add_done_callback(_on_done)
    return task


from coderai.utils.logging import log_openai_chat_completion_debug
from coderai.utils.common.file_history import GitFileHistory
from coderai.utils.common.llm_retry import (
    DEFAULT_MAX_DELAY_MS,
    DEFAULT_MAX_RETRIES,
    classify_llm_failure,
    is_empty_llm_response,
    is_failover_eligible,
    provider_retry_after_ms,
    retry_delay_ms,
)
from coderai.utils.common.openai_thinking import build_thinking_request_options

from coderai.utils.common.message_converter import OpenAIMessageConverter
from coderai.utils.common.repeat_tool_reminder import RepeatToolReminder
from coderai.mcp import McpManager
from coderai.soul.approval import (
    PermissionTicket,
    build_permission_tool_execution,
)
from coderai.soul.compaction import (
    BasicCompaction,
    DEFAULT_MAX_TOOL_RESULT_CHARS,
    ToolResultPruner,
)
from coderai.prompt import (
    get_init_command_prompt,
    get_runtime_context,
    get_system_prompt,
    load_agent_instructions,
)
from coderai.skill import (
    build_skill_documents_prompt,
    list_skills,
    load_skill,
    match_skills_for_prompt,
)
from coderai.events import (
    SessionEvent,
)
from coderai.soul.session.store import JsonlSessionStore
from coderai.soul.session.store import get_project_code as get_project_code
from coderai.state import clear_session_state
from coderai.tools.legacy.executor import ToolExecutor
from coderai.tools.legacy.types import (
    TOOL_ABORTED_BEFORE_DISPATCH,
    BackgroundProcessCompletion,
    ToolExecutionFollowUpMessage,
    ToolExecutionHooks,
    ToolResult,
)


class SessionInterrupted(Exception):
    """Raised when an active session activation is interrupted by the user or cancelled."""


MAX_ITERATIONS = 80_000
MAX_SESSION_ENTRIES = 50
from coderai.soul.session.background import (  # noqa: E402
    add_background_process_completion_message as _add_bg_completion_message_fn,
    build_background_failure_log_tail_slice,
    dispatch_due_schedules as _dispatch_due_schedules_fn,
    kill_live_processes as _kill_live_processes_fn,
    maybe_notify_task_completion as _maybe_notify_task_completion_fn,
    track_process_exit as _track_process_exit_fn,
    track_process_start as _track_process_start_fn,
)
from coderai.soul.session.fork_ops import (  # noqa: E402
    fork_session as _fork_session_fn,
    list_undo_targets as _list_undo_targets_fn,
    undo as _undo_fn,
)
from coderai.soul.session.completion import (  # noqa: E402
    build_tool_params_snippet as _build_tool_params_snippet,
    build_tool_result_snippet as _build_tool_result_snippet,
    call_stream_or_sync as _call_stream_or_sync,
    is_invisible_execution as _is_invisible_execution,
    normalize_tool_calls as _normalize_tool_calls,  # noqa: F401
    resolve_target_file_path as _resolve_target_file_path,
)


from coderai.soul.session.approval import (  # noqa: E402
    check_afk_for_session as _check_afk_for_session,  # noqa: F401
    check_auto_approve_for_session as _check_auto_approve_for_session,  # noqa: F401
    global_afk_check as _global_afk_check,  # noqa: F401
    register_session_manager,
    sanitize_repetition_loops,  # noqa: F401
    unregister_session_manager,
)


from coderai.soul.session.models import (  # noqa: E402
    SessionEntry,
    SessionMessage,
    _now,
    accumulate_usage as _accumulate_usage,  # noqa: F401
    accumulate_usage_per_model as _accumulate_usage_per_model,  # noqa: F401
    deserialize_message as _deserialize_message_fn,
    entry_from_dict as _entry_from_dict_fn,
    serialize_message as _serialize_message_fn,
    total_tokens as _total_tokens,  # noqa: F401
)


class ToolDispatchResult:
    """Outcome of one tool-call batch.

    Truth value follows ``waiting`` so existing ``if waiting`` checks keep
    working. ``stop_reason`` is set when the batch must end the turn
    (repeated identical tool calls).
    """

    __slots__ = ("waiting", "stop_reason")

    def __init__(self, waiting: bool = False, stop_reason: str | None = None) -> None:
        self.waiting = waiting
        self.stop_reason = stop_reason

    def __bool__(self) -> bool:
        return self.waiting


class SessionManager:
    def __init__(
        self,
        *,
        project_root: str,
        create_openai_client: Callable[[], dict[str, Any]],
        get_resolved_settings: Callable[[], dict[str, Any]],
        render_markdown: Callable[[str], str] | None = None,
        on_assistant_message: Callable[[SessionMessage, bool], None] | None = None,
        on_user_message: Callable[[SessionMessage], None] | None = None,
        on_session_entry_updated: Callable[[SessionEntry], None] | None = None,
        on_stream_chunk: Callable[[str], None] | None = None,
        on_thinking_chunk: Callable[[str], None] | None = None,
        on_llm_stream_progress: Callable[[dict[str, Any]], None] | None = None,
        non_interactive: bool = False,
        max_iterations: int = MAX_ITERATIONS,
    ) -> None:
        self.project_root = str(pathlib.Path(project_root).resolve())
        self.create_openai_client = create_openai_client
        self.get_resolved_settings = get_resolved_settings
        self.render_markdown = render_markdown or (lambda t: t)
        self.on_assistant_message = on_assistant_message or (lambda m, c: None)
        self.on_user_message = on_user_message
        self.on_session_entry_updated = on_session_entry_updated
        self.on_stream_chunk = on_stream_chunk
        self.on_thinking_chunk = on_thinking_chunk
        self.on_llm_stream_progress = on_llm_stream_progress
        self.non_interactive = non_interactive
        self.max_iterations = max_iterations
        self.mcp_manager = McpManager()
        self.mcp_manager.prepare(self.get_resolved_settings().get("mcpServers"))
        self.mcp_manager.set_on_tools_list_changed(self._refresh_mcp_tool_definitions)
        self._mcp_load_task: Any = None
        self.tool_executor = ToolExecutor(
            self.project_root, create_openai_client, mcp_manager=self.mcp_manager
        )
        self.refresh_plugin_tools()
        self.mcp_tool_definitions: list[dict[str, Any]] = []
        self.message_converter = OpenAIMessageConverter(
            render_init_prompt=lambda: get_init_command_prompt(self.project_root)
        )
        self._active_session_id: str | None = None
        self._override_model: str | None = None
        self._override_thinking_enabled: bool | None = None
        self._override_reasoning_effort: str | None = None
        self.session_controllers: dict[str, asyncio.Event] = {}
        self.compaction_engine = BasicCompaction(self)
        self.session_store = JsonlSessionStore(self.project_root, max_entries=MAX_SESSION_ENTRIES)
        self.file_history = GitFileHistory(
            self.project_root, str(self._storage()["project_dir"] / "file-history" / ".git")
        )
        self._repeat_reminders: dict[str, RepeatToolReminder] = {}
        self._pending_dmails: dict[str, tuple[str, int]] = {}
        # Subsystems: Jobs, Schedule, Agents (WF-A7: single ownership)
        from coderai.background.manager import get_job_store
        from coderai.schedule import get_schedule_manager
        from coderai.subagents.core import get_agent_registry

        self.job_store = get_job_store()
        sched_storage = str(self._storage()["project_dir"] / "schedule.json")
        self.schedule_manager = get_schedule_manager(sched_storage)
        self.agent_registry = get_agent_registry()

        # YOLO/AFK approval mode: yolo auto-approves all, afk auto-dismisses questions
        self._yolo_mode: bool = False
        self._afk_mode: bool = False
        self.active_agent_role: str = "default"
        self.additional_dirs: list[str] = []
        # Phase 2: per-session persisted state + soul views + approval runtime.
        from coderai.approval_runtime import ApprovalRuntime

        self.approval_runtime = ApprovalRuntime()
        # Session-level wire hub (approval/notifications fan-out)
        # + persistent notification manager (llm/wire/shell sinks).
        from coderai.wire.root_hub import RootWireHub

        self.root_wire_hub = RootWireHub()
        self.approval_runtime.bind_root_wire_hub(self.root_wire_hub)
        from coderai.notifications import NotificationManager

        try:
            _stale_ms = int(
                self.get_resolved_settings().get("notificationsClaimStaleAfterMs") or 15_000
            )
        except (TypeError, ValueError):
            _stale_ms = 15_000
        self.notification_manager = NotificationManager(
            self._storage()["project_dir"] / "notifications",
            claim_stale_after_s=max(1.0, _stale_ms / 1000.0),
        )
        try:
            _mcp_timeout_ms = int(
                self.get_resolved_settings().get("mcpToolCallTimeoutMs") or 60_000
            )
        except (TypeError, ValueError):
            _mcp_timeout_ms = 60_000
        self.mcp_manager.default_tool_timeout_s = max(1.0, _mcp_timeout_ms / 1000.0)
        self._session_states: dict[str, Any] = {}
        self._souls: dict[str, Any] = {}
        register_session_manager(self)

        # Event-model state: per-session turn/step/seq counters.
        # The lock guards all counter and message-cache mutations so
        # concurrent activations cannot mint duplicate seqs/turns/steps.
        # It is never held across awaits.
        self._seq_lock = threading.RLock()
        self._turn_counters: dict[str, int] = {}
        self._step_counters: dict[str, int] = {}
        self._seq_counters: dict[str, int] = {}
        self._checkpoint_counters: dict[str, int] = {}
        self._turn_step_synced: set[str] = set()
        # Bounded stat-keyed cache of deserialized messages: avoids a full
        # JSONL re-read/parse on every list_session_messages call in a turn.
        self._messages_cache: dict[str, tuple[int, int, list[Any]]] = {}
        self._messages_cache_bound = 64

    def set_model(self, model_name: str) -> None:
        clean = (model_name or "").strip()
        if not clean:
            raise ValueError("Model name must be a non-empty string.")
        if len(clean) > 256:
            raise ValueError("Model name must be 256 characters or fewer.")
        self._override_model = clean

    def get_active_model(self) -> str:
        if self._override_model:
            return self._override_model
        return str(self.get_resolved_settings().get("model") or "gpt-6-luna")

    def set_thinking_enabled(self, enabled: bool) -> None:
        """Session-scoped thinking-mode override (mirrors :meth:`set_model`)."""
        self._override_thinking_enabled = bool(enabled)

    def get_thinking_enabled(self) -> bool:
        """Effective thinking mode: session override, else resolved settings."""
        if self._override_thinking_enabled is not None:
            return self._override_thinking_enabled
        return bool(self.get_resolved_settings().get("thinkingEnabled"))

    def set_reasoning_effort(self, effort: str) -> None:
        from coderai.utils.common.openai_thinking import normalize_reasoning_effort

        norm = normalize_reasoning_effort(effort)
        self._override_reasoning_effort = norm

    def get_reasoning_effort(self) -> str:
        if self._override_reasoning_effort:
            return self._override_reasoning_effort
        return str(self.get_resolved_settings().get("reasoningEffort") or "max")

    def get_active_agent_role(self) -> str:
        return getattr(self, "active_agent_role", "default")

    def switch_agent_role(self, role_name: str, session_id: str | None = None) -> bool:
        """Switch the active agent role specification (e.g. architect, code-reviewer, default)."""
        clean_role = (role_name or "").strip().lower()
        if not clean_role:
            return False
        try:
            from pathlib import Path
            from coderai.agentspec import render_system_prompt, resolve_agent_spec

            spec = resolve_agent_spec(clean_role, project_root=Path(self.project_root))
            rendered = render_system_prompt(spec)
            settings = self.get_resolved_settings()
            if rendered:
                settings["persona"] = rendered
            if spec.allowed_tools is not None:
                settings["allowedTools"] = list(spec.allowed_tools)
            else:
                settings.pop("allowedTools", None)
            if spec.model:
                self.set_model(spec.model)
            self.active_agent_role = spec.name or clean_role

            target_sid = session_id or self._active_session_id
            if target_sid and rendered:
                rows = list(self.session_store.read_rows(target_sid))
                if rows:
                    updated_rows = []
                    found_system = False
                    for r in rows:
                        if not found_system and r.get("role") == "system":
                            updated_rows.append({**r, "content": rendered})
                            found_system = True
                        else:
                            updated_rows.append(r)
                    if not found_system:
                        updated_rows.insert(
                            0, {"seq": 0, "role": "system", "content": rendered, "meta": {}}
                        )
                    self.session_store.replace_rows(target_sid, updated_rows)
                    if hasattr(self, "_messages_cache") and target_sid in self._messages_cache:
                        self._messages_cache.pop(target_sid, None)

            return True
        except Exception as exc:
            import sys

            print(f"Warning: Failed to switch agent role to '{clean_role}': {exc}", file=sys.stderr)
            return False

    # ---- YOLO / AFK ----
    def is_yolo(self) -> bool:
        return self._yolo_mode

    def set_yolo(self, enabled: bool) -> None:
        self._yolo_mode = bool(enabled)
        for sid, state in self._session_states.items():
            try:
                state.approval.yolo = bool(enabled)
                self._save_session_state(sid)
            except Exception:
                continue

    def is_afk(self) -> bool:
        return self._afk_mode

    def set_afk(self, enabled: bool) -> None:
        enabled = bool(enabled)
        changed = enabled != self._afk_mode
        self._afk_mode = enabled
        for sid, state in self._session_states.items():
            try:
                state.approval.afk = enabled
                self._save_session_state(sid)
            except Exception:
                continue
        if changed:
            for soul in self._souls.values():
                try:
                    import asyncio as _asyncio

                    try:
                        loop = _asyncio.get_running_loop()
                        _track_background_task(
                            loop.create_task(soul.notify_afk_changed(enabled)),
                            "notify_afk_changed",
                        )
                    except RuntimeError:
                        pass
                except Exception:
                    continue

    def is_auto_approve(self) -> bool:
        return self._yolo_mode or self._afk_mode

    @property
    def yolo(self) -> bool:
        """Attribute alias for :meth:`is_yolo` so `/yolo` can toggle `mgr.yolo`."""
        return self._yolo_mode

    @yolo.setter
    def yolo(self, enabled: bool) -> None:
        self.set_yolo(enabled)

    @property
    def afk(self) -> bool:
        """Attribute alias for :meth:`is_afk` so `/afk` can toggle `mgr.afk`."""
        return self._afk_mode

    @afk.setter
    def afk(self, enabled: bool) -> None:
        self.set_afk(enabled)

    # ---- Phase 2: persisted session state + soul views ----
    def _session_dir(self, session_id: str) -> pathlib.Path:
        return self._storage()["project_dir"] / str(session_id)

    def get_session_state(self, session_id: str) -> Any:
        """Load (caching) the persisted :class:`SessionState` for a session."""
        from coderai.session_state import SessionState, load_session_state

        target = self.resolve_session_id(session_id) or session_id
        cached = self._session_states.get(target)
        if isinstance(cached, SessionState):
            return cached
        state = load_session_state(self._session_dir(target))
        # Resume restores persisted YOLO/AFK into the live manager. Live
        # CLI flags still win and are written back onto the session.
        if state.approval.yolo and not self._yolo_mode:
            self.set_yolo(True)
        if state.approval.afk and not self._afk_mode:
            self.set_afk(True)
        state.approval.yolo = self._yolo_mode or state.approval.yolo
        state.approval.afk = self._afk_mode or state.approval.afk
        entry = self._get_entry(target) or {}
        if entry.get("planMode") and not state.plan_mode:
            state.plan_mode = True
        if self.additional_dirs:
            for extra in self.additional_dirs:
                if extra not in state.additional_dirs:
                    state.additional_dirs.append(extra)
        self._session_states[target] = state
        return state

    def _save_session_state(self, session_id: str) -> None:
        """Persist the cached state for a session (best-effort)."""
        from coderai.session_state import save_session_state

        target = self.resolve_session_id(session_id) or session_id
        state = self._session_states.get(target)
        if state is None:
            return
        try:
            save_session_state(state, self._session_dir(target))
        except Exception:
            pass

    def get_soul(self, session_id: str, *, is_subagent: bool = False) -> Any:
        """Return the cached :class:`SessionSoul` for a session."""
        from coderai.soul.agent import SessionSoul

        target = self.resolve_session_id(session_id) or session_id
        key = f"{target}:{'sub' if is_subagent else 'root'}"
        soul = self._souls.get(key)
        if soul is None:
            soul = SessionSoul(self, target, is_subagent=is_subagent)
            self._souls[key] = soul
        return soul

    def set_plan_mode(self, session_id: str, enabled: bool) -> bool:
        """Single source of truth for plan mode: updates entry, SessionState, and soul together."""
        target = self.resolve_session_id(session_id) or session_id
        now = _now()
        self._update_entry(
            target,
            lambda e: {**e, "planMode": bool(enabled), "updateTime": now},
        )
        try:
            state = self.get_session_state(target)
            if state is not None:
                state.plan_mode = bool(enabled)
                self._save_session_state(target)
        except Exception:
            pass
        try:
            soul = self.get_soul(target)
            if soul is not None:
                if enabled:
                    soul.schedule_plan_activation_reminder()
        except Exception:
            pass
        return bool(enabled)

    def sync_session_state_from_entry(self, session_id: str) -> None:
        """Mirror index entry (title/planMode) into persisted state."""
        target = self.resolve_session_id(session_id) or session_id
        entry = self._get_entry(target) or {}
        self.set_plan_mode(target, bool(entry.get("planMode")))

    def get_diff(self, session_id: str | None = None, from_checkpoint: str | None = None) -> str:
        sid = session_id or self._active_session_id
        if not sid:
            return ""
        target_id = self.resolve_session_id(sid) or sid
        return self.file_history.get_diff(target_id, from_checkpoint=from_checkpoint)

    def grant_permission_ticket(
        self,
        tool_name: str = "*",
        scope: str = "*",
        duration_seconds: float | None = None,
        max_uses: int | None = None,
        pattern: str | None = None,
        session_id: str | None = None,
    ) -> PermissionTicket:
        """Grant a structured capability escalation ticket to a session."""
        from coderai.soul.approval import get_permission_ticket_registry

        sid = session_id or self._active_session_id or ""
        target_id = self.resolve_session_id(sid) or sid
        return get_permission_ticket_registry().request_escalation(
            session_id=target_id,
            tool_name=tool_name,
            scope=scope,
            duration_seconds=duration_seconds,
            max_uses=max_uses,
            pattern=pattern,
        )

    def list_active_permission_tickets(
        self, session_id: str | None = None
    ) -> list[PermissionTicket]:
        """List active capability escalation tickets for a session."""
        from coderai.soul.approval import get_permission_ticket_registry

        sid = session_id or self._active_session_id or ""
        target_id = self.resolve_session_id(sid) or sid
        return get_permission_ticket_registry().list_active_tickets(session_id=target_id)

    def revoke_permission_ticket(self, ticket_id: str) -> bool:
        """Revoke a capability escalation ticket by ID."""
        from coderai.soul.approval import get_permission_ticket_registry

        return get_permission_ticket_registry().revoke_ticket(ticket_id)

    # ---- storage ----

    def _storage(self) -> dict[str, pathlib.Path]:
        return self.session_store.storage_paths()

    def _messages_path(self, session_id: str) -> pathlib.Path:
        target_id = self.resolve_session_id(session_id) or session_id
        return self.session_store.messages_path(target_id)

    def _ensure_dir(self) -> pathlib.Path:
        self.session_store.project_dir.mkdir(parents=True, exist_ok=True)
        return self.session_store.project_dir

    def _load_index(self) -> dict[str, Any]:
        return self.session_store.load_index()

    def _save_index(self, index: dict[str, Any]) -> None:
        self.session_store.save_index(index)

    def _append_message(self, message: SessionMessage) -> None:
        """Append one legacy-compatible message row to the session log."""
        self.session_store.append_row(message.session_id, self._serialize_message(message))
        self._invalidate_messages_cache(message.session_id)

    def _ensure_seq_loaded_nolock(self, session_id: str) -> None:
        """Seed the in-memory seq counter from the persisted log (call with lock held)."""
        if session_id in self._seq_counters:
            return
        max_seq = 0
        try:
            for row in self.session_store.read_rows(session_id):
                seq = row.get("seq")
                if isinstance(seq, int) and seq >= max_seq:
                    max_seq = seq + 1
        except Exception:
            pass
        self._seq_counters[session_id] = max_seq

    def _next_seq(self, session_id: str) -> int:
        """Return and increment the monotonic event sequence for a session.

        Lock-guarded and seeded from the persisted log on first use, so the
        counter survives process restarts and concurrent activations.
        """
        with self._seq_lock:
            self._ensure_seq_loaded_nolock(session_id)
            seq = self._seq_counters.get(session_id, 0)
            self._seq_counters[session_id] = seq + 1
            return seq

    def _sync_turn_step_from_log_nolock(self, session_id: str) -> None:
        """Seed turn/step counters from persisted events (call with lock held)."""
        if session_id in self._turn_step_synced:
            return
        self._turn_step_synced.add(session_id)
        max_turn = self._turn_counters.get(session_id, 0)
        max_step = self._step_counters.get(session_id, 0)
        try:
            for event in self.session_store.list_events(session_id):
                data = event.data or {}
                turn = data.get("turn")
                if isinstance(turn, int) and turn > max_turn:
                    max_turn = turn
                    max_step = 0
                step = data.get("step")
                if isinstance(step, int) and step > max_step:
                    max_step = step
        except Exception:
            pass
        self._turn_counters[session_id] = max_turn
        self._step_counters[session_id] = max_step

    def _current_turn(self, session_id: str) -> int:
        with self._seq_lock:
            self._sync_turn_step_from_log_nolock(session_id)
            return self._turn_counters.get(session_id, 0)

    def _current_step(self, session_id: str) -> int:
        with self._seq_lock:
            self._sync_turn_step_from_log_nolock(session_id)
            return self._step_counters.get(session_id, 0)

    def next_turn(self, session_id: str) -> int:
        """Atomically claim the next turn number for a session (resets step)."""
        with self._seq_lock:
            self._sync_turn_step_from_log_nolock(session_id)
            turn = self._turn_counters.get(session_id, 0) + 1
            self._turn_counters[session_id] = turn
            self._step_counters[session_id] = 0
            return turn

    def next_step(self, session_id: str) -> int:
        """Atomically claim the next step number for a session."""
        with self._seq_lock:
            self._sync_turn_step_from_log_nolock(session_id)
            step = self._step_counters.get(session_id, 0) + 1
            self._step_counters[session_id] = step
            return step

    def _append_event(self, session_id: str, event: SessionEvent) -> None:
        """Append a typed SessionEvent to the JSONL log.

        Events are written in the new format alongside legacy messages.
        The JSONL line includes a ``type`` key that distinguishes it from
        legacy ``SessionMessage`` dicts (which have ``role`` instead).
        The write and the seq-counter advance happen under one lock so the
        persisted ``seq`` order always matches the mint order.
        """
        with self._seq_lock:
            self.session_store.append_row(session_id, event.to_dict())
            current = self._seq_counters.get(session_id, 0)
            if isinstance(event.seq, int) and event.seq + 1 > current:
                self._seq_counters[session_id] = event.seq + 1
            self._messages_cache.pop(session_id, None)

    def _invalidate_messages_cache(self, session_id: str) -> None:
        # No index resolution here: this runs on the append hot path and the
        # row was written under session_id already.
        with self._seq_lock:
            self._messages_cache.pop(session_id, None)

    def _save_messages(self, session_id: str, messages: list[SessionMessage]) -> None:
        target_id = self.resolve_session_id(session_id) or session_id
        self.session_store.replace_rows(
            target_id, [self._serialize_message(message) for message in messages]
        )
        with self._seq_lock:
            self._messages_cache.pop(target_id, None)

    def _messages_cache_key(self, session_id: str) -> tuple[int, int] | None:
        try:
            stat = self.session_store.messages_path(session_id).stat()
            return (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return None

    def list_session_messages(self, session_id: str) -> list[SessionMessage]:
        target_id = self.resolve_session_id(session_id) or session_id
        with self._seq_lock:
            key = self._messages_cache_key(target_id)
            if key is not None:
                hit = self._messages_cache.get(target_id)
                if hit is not None and (hit[0], hit[1]) == key:
                    return list(hit[2])
            messages: list[SessionMessage] = []
            max_seq = self._seq_counters.get(target_id, 0)
            for row in self.session_store.read_rows(target_id):
                if "seq" in row and isinstance(row["seq"], int):
                    max_seq = max(max_seq, row["seq"] + 1)
                message = self._deserialize_message(row, target_id)
                if message is not None:
                    messages.append(message)
            self._seq_counters[target_id] = max_seq
            if key is not None:
                if len(self._messages_cache) >= self._messages_cache_bound:
                    self._messages_cache.pop(next(iter(self._messages_cache)), None)
                self._messages_cache[target_id] = (key[0], key[1], list(messages))
            return messages

    def list_session_events(self, session_id: str) -> list[SessionEvent]:
        target_id = self.resolve_session_id(session_id) or session_id
        return self.session_store.list_events(target_id)

    def _serialize_message(self, m: SessionMessage) -> dict[str, Any]:
        return _serialize_message_fn(m)

    def _deserialize_message(self, d: dict[str, Any], session_id: str) -> SessionMessage | None:
        return _deserialize_message_fn(d, session_id)

    def resolve_session_id(self, session_id: str | None, *, fuzzy: bool = False) -> str | None:
        """Resolve a session ID, short prefix, or checkpoint hash to canonical full session ID."""
        if not session_id or not isinstance(session_id, str):
            return None
        sid = session_id.strip()
        if not sid:
            return None

        entries = self._load_index().get("entries", [])
        if not entries:
            return None

        # 1. Exact match by session ID
        for entry in entries:
            eid = entry.get("id", "")
            if eid == sid:
                return eid

        if not fuzzy:
            return None

        # 2. Case-insensitive prefix match by session ID (unambiguous only)
        sid_lower = sid.lower()
        prefix_matches = [
            entry.get("id", "")
            for entry in entries
            if entry.get("id", "").lower().startswith(sid_lower)
        ]
        if len(prefix_matches) == 1:
            return prefix_matches[0]
        if len(prefix_matches) > 1:
            return None

        # 3. Match by checkpoint hash prefix in Git file history
        for entry in entries:
            eid = entry.get("id", "")
            if not eid:
                continue
            try:
                cur_ref = self.file_history.get_current_checkpoint_hash(eid)
                if cur_ref and cur_ref.lower().startswith(sid_lower):
                    return eid
            except Exception:
                pass

        # 4. Check in turn message metadata checkpoint hashes
        for entry in entries:
            eid = entry.get("id", "")
            if not eid:
                continue
            try:
                messages = self.session_store.read_rows(eid)
                for m in messages:
                    meta = m.get("meta") or {}
                    ckpt = meta.get("checkpointHash")
                    if ckpt and str(ckpt).lower().startswith(sid_lower):
                        return eid
            except Exception:
                pass

        # 5. Fuzzy match / substring in session ID if len >= 4 (unambiguous only)
        if len(sid) >= 4:
            fuzzy_matches = [
                entry.get("id", "") for entry in entries if sid_lower in entry.get("id", "").lower()
            ]
            if len(fuzzy_matches) == 1:
                return fuzzy_matches[0]
            if len(fuzzy_matches) > 1:
                return None

        return None

    def _update_entry(
        self, session_id: str, mutate: Callable[[dict[str, Any]], dict[str, Any]]
    ) -> bool:
        target_id = self.resolve_session_id(session_id) or session_id
        index = self._load_index()
        for i, entry in enumerate(index.get("entries", [])):
            if entry.get("id") == target_id:
                index["entries"][i] = mutate(entry)
                self._save_index(index)
                if self.on_session_entry_updated:
                    self.on_session_entry_updated(_entry_from_dict(index["entries"][i]))
                return True
        return False

    def _get_entry(self, session_id: str) -> dict[str, Any] | None:
        if not session_id:
            return None
        target_id = self.resolve_session_id(session_id) or session_id
        for entry in self._load_index().get("entries", []):
            if entry.get("id") == target_id:
                return entry
        return None

    # ---- builders ----

    def _build_message(
        self, session_id: str, role: str, content: str, **kwargs: Any
    ) -> SessionMessage:
        now = _now()
        return SessionMessage(
            id=uuid.uuid4().hex,
            session_id=session_id,
            role=role,
            content=content,
            create_time=now,
            update_time=now,
            **kwargs,
        )

    def _build_assistant(
        self,
        session_id: str,
        content: str,
        tool_calls: list[Any] | None,
        thinking: str | None = None,
    ) -> SessionMessage:
        return self._build_message(
            session_id, "assistant", content, tool_calls=tool_calls, thinking=thinking
        )

    def _build_tool_message(
        self,
        session_id: str,
        tool_call_id: str,
        content: str,
        tool_function: Any = None,
        tool_meta: dict[str, Any] | None = None,
    ) -> SessionMessage:
        now = _now()
        pruned_content = ToolResultPruner(max_chars=DEFAULT_MAX_TOOL_RESULT_CHARS).prune_content(
            content
        )
        is_invisible = _is_invisible_execution(pruned_content)
        params_md = _build_tool_params_snippet(tool_function)
        result_md = _build_tool_result_snippet(pruned_content)
        meta: dict[str, Any] = {
            "function": tool_function,
            "paramsMd": params_md,
            "resultMd": result_md,
        }
        if tool_meta and isinstance(tool_meta, dict):
            meta.update(tool_meta)
        return SessionMessage(
            id=uuid.uuid4().hex,
            session_id=session_id,
            role="tool",
            content=pruned_content,
            tool_call_id=tool_call_id,
            compacted=False,
            visible=not is_invisible,
            create_time=now,
            update_time=now,
            meta=meta,
        )

    # ---- lifecycle ----

    def interrupt_session(self, session_id: str) -> None:
        """Interrupt and cancel a running session.

        Signals the existing controller in place so a running activation
        observing its own event sees the interrupt; only installs a preset
        event when no activation owns one.
        """
        ctrl = self.session_controllers.get(session_id)
        if ctrl is None:
            ctrl = asyncio.Event()
            ctrl.set()
            self.session_controllers[session_id] = ctrl
        else:
            ctrl.set()
        self.kill_live_processes(session_id)
        clear_session_state(session_id)
        self._update_entry(
            session_id,
            lambda e: {
                **e,
                "status": "interrupted",
                "failReason": "interrupted",
                "updateTime": _now(),
            },
        )

    def is_interrupted(self, session_id: str) -> bool:
        ctrl = self.session_controllers.get(session_id)
        if ctrl and ctrl.is_set():
            return True
        entry = self._get_entry(session_id)
        return bool(entry and entry.get("status") in ("interrupted", "failed"))

    def build_background_failure_log_tail_slice(self, output_path: str | None) -> str | None:
        return build_background_failure_log_tail_slice(output_path)

    def _dispatch_due_schedules(self, session_id: str) -> bool:
        return _dispatch_due_schedules_fn(self, session_id)

    def add_background_process_completion_message(
        self, session_id: str, completion: BackgroundProcessCompletion
    ) -> None:
        _add_bg_completion_message_fn(self, session_id, completion)

    def _track_process_start(self, session_id: str, pid: int | str, command: str) -> None:
        _track_process_start_fn(self, session_id, pid, command)

    def _track_process_exit(self, session_id: str, pid: int | str) -> None:
        _track_process_exit_fn(self, session_id, pid)

    def kill_live_processes(self, session_id: str | None = None) -> None:
        _kill_live_processes_fn(self, session_id)

    def maybe_notify_task_completion(self, session_id: str, started_at_ms: int) -> None:
        _maybe_notify_task_completion_fn(self, session_id, started_at_ms)

    def _build_index_entry(
        self,
        session_id: str,
        summary: str,
        *,
        status: str = "pending",
        plan_mode: bool = False,
    ) -> dict[str, Any]:
        """Build a canonical index entry for session storage (AL-B8)."""
        now = _now()
        return {
            "id": session_id,
            "summary": summary,
            "assistantReply": None,
            "assistantThinking": None,
            "assistantRefusal": None,
            "toolCalls": None,
            "status": status,
            "failReason": None,
            "usage": None,
            "usagePerModel": None,
            "activeTokens": 0,
            "processes": {},
            "createTime": now,
            "updateTime": now,
            "planMode": plan_mode,
        }

    def _record_index_entry(self, entry: dict[str, Any]) -> None:
        """Insert entry into session index, sorted by updateTime, capped to MAX_SESSION_ENTRIES."""
        index = self._load_index()
        index["entries"].append(entry)
        index["entries"] = sorted(
            index["entries"], key=lambda e: e.get("updateTime", ""), reverse=True
        )[:MAX_SESSION_ENTRIES]
        self._save_index(index)

    def _create_empty_session(self, plan_mode: bool = False) -> str:
        session_id = uuid.uuid4().hex
        entry = self._build_index_entry(
            session_id, "New Session", status="ready", plan_mode=plan_mode
        )
        self._record_index_entry(entry)
        self.file_history.ensure_session(session_id)
        self._active_session_id = session_id
        try:
            state = self.get_session_state(session_id)
            state.plan_mode = bool(plan_mode)
            self._save_session_state(session_id)
        except Exception:
            pass
        try:  # Remember latest session per workdir.
            from coderai.metadata import record_last_session

            record_last_session(self.project_root, session_id)
        except Exception:
            pass
        return session_id

    def steer_session(self, session_id: str, text: str) -> None:
        if not hasattr(self, "_steer_queues"):
            self._steer_queues = {}
        self._steer_queues.setdefault(session_id, []).append(text)

    def pop_steers(self, session_id: str) -> list[str]:
        if not hasattr(self, "_steer_queues"):
            return []
        return self._steer_queues.pop(session_id, [])

    def context_checkpoint_count(self, session_id: str) -> int:
        """How many context checkpoints exist (next id, and D-Mail bound)."""
        target = self.resolve_session_id(session_id) or session_id
        with self._seq_lock:
            if target in self._checkpoint_counters:
                return self._checkpoint_counters[target]
            count = 0
            try:
                rows = self.session_store.read_rows(target)
                for row in rows:
                    if row.get("role") != "_checkpoint":
                        continue
                    checkpoint_id = row.get("id")
                    if isinstance(checkpoint_id, int):
                        count = max(count, checkpoint_id + 1)
            except Exception:
                pass
            self._checkpoint_counters[target] = count
            return count

    def checkpoint_context(self, session_id: str) -> int:
        """Append a context checkpoint marker and return its id.

        The marker is not a model message. Reverting to this id drops the
        marker and every row written after it.
        """
        target = self.resolve_session_id(session_id) or session_id
        with self._seq_lock:
            checkpoint_id = self.context_checkpoint_count(target)
            self._checkpoint_counters[target] = checkpoint_id + 1
            self.session_store.append_row(target, {"role": "_checkpoint", "id": checkpoint_id})
            self._invalidate_messages_cache(target)
            return checkpoint_id

    async def revert_context_to(self, session_id: str, checkpoint_id: int) -> None:
        """Rotate the session log and keep only rows before ``checkpoint_id``."""
        from coderai.soul.compaction import estimate_text_tokens
        from coderai.utils.path import next_available_rotation

        target = self.resolve_session_id(session_id) or session_id
        path = self.session_store.messages_path(target)
        rows = self.session_store.read_rows(target)
        cut: int | None = None
        for index, row in enumerate(rows):
            if row.get("role") == "_checkpoint" and row.get("id") == checkpoint_id:
                cut = index
                break
        if cut is None:
            raise ValueError(f"Checkpoint {checkpoint_id} does not exist")
        rotated = await next_available_rotation(path)
        if rotated is None:
            raise RuntimeError("No available rotation path found")
        path.replace(rotated)
        self.session_store.replace_rows(target, rows[:cut])
        self._invalidate_messages_cache(target)
        with self._seq_lock:
            self._checkpoint_counters[target] = checkpoint_id
        estimated = estimate_text_tokens(self.list_session_messages(target))
        self._update_entry(
            target,
            lambda entry, tokens=estimated: {
                **entry,
                "activeTokens": tokens,
                "updateTime": _now(),
            },
        )

    def stage_dmail(self, session_id: str, message: str, checkpoint_id: int) -> None:
        """Validate and hold one D-Mail until the turn loop rewinds."""
        from coderai.soul.denwarenji import DenwaRenjiError

        target = self.resolve_session_id(session_id) or session_id
        if target in self._pending_dmails:
            raise DenwaRenjiError("Only one D-Mail can be sent at a time")
        if checkpoint_id < 0:
            raise DenwaRenjiError("The checkpoint ID can not be negative")
        if checkpoint_id >= self.context_checkpoint_count(target):
            raise DenwaRenjiError("There is no checkpoint with the given ID")
        text = message.strip()
        if not text:
            raise DenwaRenjiError("D-Mail message is empty")
        self._pending_dmails[target] = (text[:4000], checkpoint_id)

    def take_pending_dmail(self, session_id: str) -> tuple[str, int] | None:
        """Take the staged D-Mail, if any."""
        target = self.resolve_session_id(session_id) or session_id
        return self._pending_dmails.pop(target, None)

    async def respond_permissions(
        self,
        session_id: str,
        replies: list[dict[str, Any]],
        plan_mode: bool | None = None,
        user_prompt: str | None = None,
    ) -> None:
        """Respond to pending permission requests in a session."""
        await self.reply_session(
            session_id,
            user_prompt=user_prompt,
            permission_replies=replies,
            plan_mode=plan_mode,
        )

    async def create_session(
        self,
        user_prompt: str,
        plan_mode: bool = False,
        skills: list[str] | None = None,
        content_params: list[dict[str, Any]] | None = None,
    ) -> str:
        session_id = uuid.uuid4().hex
        # When max_ralph_iterations != 0, turn the prompt into an
        # automated repeat loop instead of a single turn (checked up front so
        # the prompt is not appended twice).
        try:
            from coderai.skill.flow.runner import ralph_iterations_for_prompt

            ralph_iterations = ralph_iterations_for_prompt(
                self.get_resolved_settings(), user_prompt
            )
        except Exception:
            ralph_iterations = 0
        self._repeat_reminders.pop(session_id, None)
        summary = (user_prompt or "[Image Prompt]")[:100]
        entry = self._build_index_entry(session_id, summary, status="pending", plan_mode=plan_mode)
        self._record_index_entry(entry)

        # File history session checkpoint. The branch is brand-new (fresh uuid),
        # so its manifest is empty and a tracked-files record would be a no-op
        # returning the initial hash (~3 wasted git spawns on the TTFT path).
        # Use the initial commit hash directly.
        initial_hash = self.file_history.ensure_session(session_id)
        ckpt_hash = initial_hash

        model = self.get_active_model()
        settings = self.get_resolved_settings()
        sandbox_mode = (settings.get("permissions") or {}).get("sandbox")
        instructions = load_agent_instructions(self.project_root)
        prompt_options = {
            "model": model,
            "nonInteractive": self.non_interactive,
            "sandboxMode": sandbox_mode,
            "workspaceRoot": self.project_root,
            "persona": settings.get("persona"),
            "preset": settings.get("preset") or settings.get("toolsPreset"),
            "enabledSkills": settings.get("enabledSkills"),
            "skillScanPaths": settings.get("skillScanPaths"),
            "instructions": instructions,
            "planMode": plan_mode,
        }
        self._append_message(
            self._build_message(
                session_id,
                "system",
                get_system_prompt(prompt_options),
                meta={"isPlanMode": bool(plan_mode)},
            )
        )

        if ralph_iterations == 0:
            # Prepend dynamic workspace runtime context to the first user turn (keeping system prompt prefix 100% static)
            runtime_context = get_runtime_context(self.project_root, model)
            if runtime_context:
                if isinstance(user_prompt, str):
                    effective_user_prompt = f"{runtime_context}\n\n---\n\n{user_prompt}"
                elif isinstance(user_prompt, dict):
                    effective_user_prompt = dict(user_prompt)
                    effective_user_prompt["text"] = (
                        f"{runtime_context}\n\n---\n\n{user_prompt.get('text', '')}"
                    )
                else:
                    effective_user_prompt = f"{runtime_context}\n\n---\n\n{str(user_prompt)}"
            else:
                effective_user_prompt = user_prompt

            self._append_message(
                self._build_message(
                    session_id,
                    "user",
                    effective_user_prompt,
                    meta={
                        "checkpointHash": ckpt_hash,
                        "userPrompt": {"planMode": plan_mode},
                        "rawPrompt": user_prompt,
                        **({"contentParams": list(content_params)} if content_params else {}),
                    },
                )
            )
            await self._inject_matched_skills(session_id, user_prompt, skills)
        self._active_session_id = session_id
        # SessionStart fires on creation.
        try:
            from coderai.hooks import run_session_start

            run_session_start(session_id, self.project_root, "create")
        except Exception:
            pass
        try:  # Remember latest session per workdir.
            from coderai.metadata import record_last_session

            record_last_session(self.project_root, session_id)
        except Exception:
            pass
        if ralph_iterations != 0:
            from coderai.skill.flow.runner import FlowRunner

            text = user_prompt if isinstance(user_prompt, str) else str(user_prompt or "")
            await FlowRunner.ralph_loop(text.strip(), ralph_iterations).run(self, session_id)
            return session_id
        await self._activate(session_id)
        return session_id

    async def create_empty_session(self, plan_mode: bool = False) -> str:
        """Create a session with only the system prompt (no turn runs).

        Used by flow runs (``/flow:`` / ralph), which append their own turns.
        Mirrors the ``create_session`` prologue without the user message.
        """
        session_id = uuid.uuid4().hex
        self._repeat_reminders.pop(session_id, None)
        entry = self._build_index_entry(
            session_id, "[Flow session]", status="pending", plan_mode=plan_mode
        )
        self._record_index_entry(entry)

        self.file_history.ensure_session(session_id)

        model = self.get_active_model()
        settings = self.get_resolved_settings()
        sandbox_mode = (settings.get("permissions") or {}).get("sandbox")
        instructions = load_agent_instructions(self.project_root)
        prompt_options = {
            "model": model,
            "nonInteractive": self.non_interactive,
            "sandboxMode": sandbox_mode,
            "workspaceRoot": self.project_root,
            "persona": settings.get("persona"),
            "preset": settings.get("preset") or settings.get("toolsPreset"),
            "enabledSkills": settings.get("enabledSkills"),
            "skillScanPaths": settings.get("skillScanPaths"),
            "instructions": instructions,
            "planMode": plan_mode,
        }
        self._append_message(
            self._build_message(
                session_id,
                "system",
                get_system_prompt(prompt_options),
                meta={"isPlanMode": bool(plan_mode)},
            )
        )
        self._active_session_id = session_id
        try:
            from coderai.hooks import run_session_start

            run_session_start(session_id, self.project_root, "create")
        except Exception:
            pass
        try:
            from coderai.metadata import record_last_session

            record_last_session(self.project_root, session_id)
        except Exception:
            pass
        return session_id

    def is_continue_prompt(self, user_prompt: Any) -> bool:
        """Check if a prompt is an affirmative continuation request."""
        if not user_prompt:
            return False
        if isinstance(user_prompt, str):
            text = user_prompt.strip().lower()
            return text in ("/continue", "continue", "go on", "proceed")
        if isinstance(user_prompt, dict):
            text = str(user_prompt.get("text", "")).strip().lower()
            has_images = bool(user_prompt.get("imageUrls") or user_prompt.get("images"))
            has_skills = bool(user_prompt.get("skills"))
            return (
                text in ("/continue", "continue", "go on", "proceed")
                and not has_images
                and not has_skills
            )
        return False

    async def reply_session(
        self,
        session_id: str,
        user_prompt: str | None = None,
        permission_replies: list[dict[str, Any]] | None = None,
        plan_mode: bool | None = None,
        skills: list[str] | None = None,
        content_params: list[dict[str, Any]] | None = None,
    ) -> None:
        entry = self._get_entry(session_id)
        if not entry:
            await self.create_session(
                user_prompt or "",
                plan_mode=bool(plan_mode),
                skills=skills,
                content_params=content_params,
            )
            return

        if plan_mode is not None:
            prev_mode = bool(entry.get("planMode"))
            if prev_mode != plan_mode:
                self._update_entry(
                    session_id,
                    lambda e: {**e, "planMode": plan_mode, "updateTime": _now()},
                )
                if plan_mode:
                    self._append_message(
                        self._build_message(
                            session_id,
                            "user",
                            "[Plan Mode enabled: explore and draft proposed plan before mutating repo state.]",
                            meta={"isPlanMode": True},
                        )
                    )
                else:
                    self._append_message(
                        self._build_message(
                            session_id,
                            "user",
                            "[Exited Plan Mode: proceeding with plan execution.]",
                            meta={"isPlanMode": False},
                        )
                    )
                # Phase 2: persist plan-mode flips + schedule the activation
                # reminder for the next LLM step.
                try:
                    state = self.get_session_state(session_id)
                    state.plan_mode = bool(plan_mode)
                    self._save_session_state(session_id)
                    if plan_mode:
                        self.get_soul(session_id).schedule_plan_activation_reminder()
                except Exception:
                    pass

        # Handle /continue without appending redundant user message
        is_continue = self.is_continue_prompt(user_prompt)

        if permission_replies is not None:
            # If user provided a message alongside permission replies, queue it as deferred prompt
            deferred_prompt = user_prompt if (user_prompt and not is_continue) else None
            await self._activate(
                session_id, permission_replies=permission_replies, deferred_prompt=deferred_prompt
            )
            return

        if (user_prompt or content_params) and not is_continue:
            # Image-only turns carry no text; the images ride in contentParams
            # (same path as the CLI /image command) so they still append.
            user_text = user_prompt or ""
            # UserPromptSubmit fires before the turn starts; hooks
            # may inject additionalContext (appended) or deny (abort turn).
            try:
                from coderai.hooks import run_user_prompt_submit

                _ups = run_user_prompt_submit(
                    str(user_prompt),
                    session_id,
                    self.project_root,
                    self.get_resolved_settings(),
                )
                if _ups.decision == "deny":
                    self._append_message(
                        self._build_message(
                            session_id,
                            "user",
                            f"[UserPromptSubmit denied: {_ups.reason or 'blocked by hook.'}]",
                            meta={"isHookDenial": True},
                        )
                    )
                    return
                for _ctx in _ups.additional_context:
                    self._append_message(
                        self._build_message(
                            session_id, "user", str(_ctx), meta={"isHookContext": True}
                        )
                    )
                for _sm in _ups.system_messages:
                    self._append_message(
                        self._build_message(
                            session_id, "system", str(_sm), meta={"isHookSystem": True}
                        )
                    )
            except Exception:
                pass
            # When max_ralph_iterations != 0, run the automated repeat
            # loop instead of a single turn (after UserPromptSubmit so hooks
            # still see the prompt, before anything is appended).
            try:
                from coderai.skill.flow.runner import maybe_run_ralph

                if await maybe_run_ralph(self, session_id, str(user_prompt)):
                    self._active_session_id = session_id
                    return
            except Exception as ralph_err:
                logger.warning("maybe_run_ralph failed for session %s: %s", session_id, ralph_err)
            if session_id in self._repeat_reminders:
                self._repeat_reminders[session_id].reset()
            self.file_history.ensure_session(session_id)
            ckpt_res = self.file_history.record_tracked_files_checkpoint(
                session_id, "User prompt checkpoint"
            )
            curr_entry = self._get_entry(session_id)
            curr_mode = bool(curr_entry.get("planMode")) if curr_entry else False
            self._append_message(
                self._build_message(
                    session_id,
                    "user",
                    user_text,
                    meta={
                        "checkpointHash": ckpt_res.checkpoint_hash,
                        "userPrompt": {"planMode": curr_mode},
                        **({"contentParams": list(content_params)} if content_params else {}),
                    },
                )
            )
            await self._inject_matched_skills(session_id, user_text, skills)
        elif skills:
            self._append_skill_messages(session_id, skills)

        self._active_session_id = session_id
        await self._activate(session_id)
        await self._maybe_drive_goal_rounds(session_id)

    async def _maybe_drive_goal_rounds(self, session_id: str, _depth: int = 0) -> None:
        """Automatic goal rounds (no-op; harness driver removed)."""
        return

    def list_available_skills(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Discover skills with enabledSkills filtering, custom scan paths, and loaded flags."""
        settings = self.get_resolved_settings()
        enabled = settings.get("enabledSkills") or {}
        custom_paths = settings.get("skillScanPaths") or []
        skills = list_skills(
            self.project_root,
            enabled_skills=enabled,
            custom_scan_paths=custom_paths,
            merge_all_available_skills=settings.get("mergeAllAvailableSkills", True),
        )
        loaded = self._loaded_skill_names(session_id) if session_id else set()
        for skill in skills:
            skill["isLoaded"] = skill["name"] in loaded
        return skills

    def _loaded_skill_names(self, session_id: str) -> set[str]:
        names: set[str] = set()
        for message in self.list_session_messages(session_id):
            skill_meta = (message.meta or {}).get("skill")
            if isinstance(skill_meta, dict) and isinstance(skill_meta.get("name"), str):
                names.add(skill_meta["name"])
        return names

    def _append_skill_messages(self, session_id: str, skill_names: list[str]) -> None:
        loaded = self._loaded_skill_names(session_id)
        settings = self.get_resolved_settings()
        custom_paths = settings.get("skillScanPaths") or []
        for name in skill_names:
            if not name or name in loaded:
                continue
            skill = load_skill(name, self.project_root, custom_scan_paths=custom_paths)
            if not skill:
                continue
            prompt = build_skill_documents_prompt([skill])
            if not prompt:
                continue
            message = self._build_message(
                session_id,
                "system",
                prompt,
                meta={"skill": {"name": skill["name"], "path": skill.get("path")}},
            )
            self._append_message(message)
            loaded.add(skill["name"])

    async def _inject_matched_skills(
        self,
        session_id: str,
        user_prompt: str | None,
        explicit_names: list[str] | None = None,
    ) -> None:
        names: list[str] = []
        for name in explicit_names or []:
            if name and name not in names:
                names.append(name)

        if (
            explicit_names is None
            and user_prompt
            and isinstance(user_prompt, str)
            and user_prompt.strip()
            and user_prompt.strip() != "/continue"
        ):
            settings = self.get_resolved_settings()
            enabled = settings.get("enabledSkills") or {}
            custom_paths = settings.get("skillScanPaths") or []
            loaded = self._loaded_skill_names(session_id)
            matched = match_skills_for_prompt(
                user_prompt,
                self.project_root,
                enabled_skills=enabled,
                loaded_names=loaded,
                custom_scan_paths=custom_paths,
                merge_all_available_skills=settings.get("mergeAllAvailableSkills", True),
            )
            for skill in matched:
                if skill["name"] not in names:
                    names.append(skill["name"])

        if names:
            self._append_skill_messages(session_id, names)

    def inject_skills(self, session_id: str, skill_names: list[str]) -> None:
        """Append skill documents to a session without starting a new agent turn."""
        self._append_skill_messages(session_id, skill_names)

    def load_skill_by_name(self, session_id: str, skill_name: str) -> ToolResult:
        """Dynamically load and inject a skill by exact name into the active session."""
        skills = self.list_available_skills(session_id)
        skill = next((c for c in skills if c.get("name") == skill_name), None)
        if not skill:
            return ToolResult(
                ok=False,
                name="skill",
                error=f"Unknown skill: {skill_name}. Check the available skills catalog for exact skill names.",
            )
        loaded = self._loaded_skill_names(session_id)
        if skill_name in loaded:
            return ToolResult(
                ok=True,
                name="skill",
                output=f"Skill already loaded: {skill_name}.",
            )
        self._append_skill_messages(session_id, [skill_name])
        return ToolResult(
            ok=True,
            name="skill",
            output=f"Loaded skill: {skill_name}.",
        )

    async def _activate(
        self,
        session_id: str,
        permission_replies: list[dict[str, Any]] | None = None,
        deferred_prompt: str | None = None,
    ) -> None:
        from coderai.soul.coderaisoul import AgentLoop
        from coderai.subagents.core import (
            register_session_notice_sink,
            unregister_session_notice_sink,
        )

        def _notice_sink(sid: str, text: str) -> None:
            # Live-manager notice appender: routes background settlement
            # notices into the parent conversation (harness subagent-settled /
            # job completion notices).
            try:
                message = self._build_message(
                    sid,
                    "user",
                    text,
                    meta={"advisoryRole": "user", "source": "subagent-settled"},
                )
                self._append_message(message)
            except Exception:
                pass

        register_session_notice_sink(session_id, _notice_sink)
        try:
            await self._deliver_llm_notifications(session_id)
            await self._await_mcp_ready()
            await AgentLoop(self, session_id).run(
                permission_replies=permission_replies,
                deferred_prompt=deferred_prompt,
            )
        finally:
            unregister_session_notice_sink(session_id)

    def notify(
        self,
        title: str,
        body: str = "",
        *,
        category: str = "system",
        type: str = "notice",
        source_kind: str = "session",
        source_id: str = "",
        severity: str = "info",
        targets: list[str] | None = None,
        dedupe_key: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        """Publish a notification."""
        from coderai.notifications import NotificationEvent, to_wire_notification

        manager = getattr(self, "notification_manager", None)
        if manager is None:
            return None
        event = NotificationEvent(
            id=manager.new_id(),
            category=category,
            type=type,
            source_kind=source_kind,
            source_id=source_id or "",
            title=title,
            body=body,
            severity=severity,
            targets=list(targets) if targets else ["llm", "wire", "shell"],
            dedupe_key=dedupe_key,
            payload=dict(payload or {}),
        )
        view = manager.publish(event)
        try:
            from coderai.wire.emitter import get_emitter

            get_emitter().send(to_wire_notification(view))
        except Exception:
            pass
        return view

    async def _deliver_llm_notifications(self, session_id: str) -> None:
        """Claim up to 4 pending ``llm`` notifications into this turn.

        Runs at activation start (delivers at step start; same effect for
        the first step without loop surgery). Already-seen ids are acked
        without re-appending; each delivery fires Notification hooks.
        """
        from coderai.notifications import (
            TURN_DELIVER_LIMIT,
            build_notification_message,
            extract_notification_ids,
        )

        manager = getattr(self, "notification_manager", None)
        if manager is None or not manager.has_pending_for_sink("llm"):
            return
        try:
            seen = extract_notification_ids(
                [
                    str(m.content or "")
                    for m in self.list_session_messages(session_id)
                    if getattr(m, "role", "") == "user"
                ]
            )
        except Exception:
            seen = set()

        async def _handle(view: Any) -> None:
            if view.event.id in seen:
                return
            try:
                from coderai.hooks import run_notification

                run_notification(
                    session_id,
                    self.project_root,
                    "llm",
                    view.event.type,
                    title=view.event.title,
                    body=view.event.body,
                    severity=view.event.severity,
                    settings=self.get_resolved_settings(),
                )
            except Exception:
                pass
            self._append_message(
                self._build_message(
                    session_id,
                    "user",
                    build_notification_message(view),
                    meta={"isNotification": True, "notificationId": view.event.id},
                )
            )

        try:
            await manager.deliver_pending("llm", on_notification=_handle, limit=TURN_DELIVER_LIMIT)
        except Exception:
            pass

    async def _append_tool_messages(
        self,
        session_id: str,
        tool_calls: list[Any],
        permission_replies: list[dict[str, Any]] | None = None,
        message_permissions: list[dict[str, Any]] | None = None,
    ) -> ToolDispatchResult:
        waiting = False
        force_stop = False
        ctrl = self.session_controllers.get(session_id)
        if not tool_calls:
            return ToolDispatchResult(waiting=False)

        read_only_tool_names = {
            "read",
            "Read",
            "read_file",
            "grep",
            "Grep",
            "glob",
            "Glob",
            "read_media_file",
            "WebSearch",
            "web_search",
            "WebFetch",
            "web_fetch",
            "UnderstandImage",
            "understand_image",
        }

        # Normalize all raw tool calls
        normalized_calls: list[dict[str, Any]] = []
        for raw_tc in tool_calls:
            tc = (
                raw_tc
                if isinstance(raw_tc, dict)
                else {
                    "id": getattr(raw_tc, "id", "") or uuid.uuid4().hex,
                    "type": "function",
                    "function": {
                        "name": getattr(getattr(raw_tc, "function", None), "name", "") or "",
                        "arguments": getattr(getattr(raw_tc, "function", None), "arguments", "")
                        or "",
                    },
                }
            )
            tc["id"] = str(tc.get("id") or uuid.uuid4().hex)
            normalized_calls.append(tc)

        completed_tool_call_ids: set[str] = set()

        # Partition into contiguous execution chunks (parallel vs sequential vs barrier)
        chunks: list[tuple[str, list[dict[str, Any]]]] = []
        current_chunk_kind: str | None = None
        current_chunk: list[dict[str, Any]] = []

        for tc in normalized_calls:
            fn = tc.get("function") or {}
            fn_name = str(fn.get("name", "") if isinstance(fn, dict) else "")
            fn_args_raw = fn.get("arguments", "{}") if isinstance(fn, dict) else "{}"
            parsed_args: dict[str, Any] = {}
            if isinstance(fn_args_raw, dict):
                parsed_args = fn_args_raw
            elif isinstance(fn_args_raw, str) and fn_args_raw.strip():
                try:
                    parsed_args = json.loads(fn_args_raw)
                except Exception:
                    parsed_args = {}

            blocked = build_permission_tool_execution(tc, permission_replies, message_permissions)

            if blocked:
                kind = "blocked"
            else:
                tool_def = (
                    self.tool_executor.registry.get(fn_name)
                    if hasattr(self.tool_executor, "registry") and self.tool_executor.registry
                    else None
                )
                if tool_def:
                    mode = tool_def.check_execution_mode(parsed_args)
                    if mode == "barrier":
                        kind = "barrier"
                    elif mode == "parallel":
                        kind = "parallel"
                    else:
                        kind = "sequential"
                elif fn_name in read_only_tool_names:
                    kind = "parallel"
                else:
                    kind = "sequential"

            if kind == "barrier":
                if current_chunk and current_chunk_kind is not None:
                    chunks.append((current_chunk_kind, current_chunk))
                    current_chunk = []
                    current_chunk_kind = None
                chunks.append(("barrier", [tc]))
            elif current_chunk_kind is None or current_chunk_kind == kind:
                current_chunk_kind = kind
                current_chunk.append(tc)
            else:
                chunks.append((current_chunk_kind, current_chunk))
                current_chunk_kind = kind
                current_chunk = [tc]

        if current_chunk and current_chunk_kind is not None:
            chunks.append((current_chunk_kind, current_chunk))

        should_conclude_turn = False

        try:
            for chunk_kind, chunk_tcs in chunks:
                if ctrl and ctrl.is_set():
                    break
                if should_conclude_turn:
                    break

                if chunk_kind == "blocked":
                    for tc in chunk_tcs:
                        blocked = build_permission_tool_execution(
                            tc, permission_replies, message_permissions
                        )
                        blocked_content = blocked["content"] if blocked else "Blocked by permission"
                        tool_msg = self._build_tool_message(
                            session_id,
                            tc["id"],
                            blocked_content,
                            tc.get("function"),
                        )
                        self._append_message(tool_msg)
                        self.on_assistant_message(tool_msg, True)
                        completed_tool_call_ids.add(tc["id"])
                    continue

                # Hooks for mutating calls and execution lifecycle
                target_paths: dict[str, str] = {}
                for tc in chunk_tcs:
                    func = tc.get("function") if isinstance(tc, dict) else None
                    name = str(func.get("name", "")) if isinstance(func, dict) else ""
                    if name in ("edit", "Edit", "write", "Write"):
                        try:
                            tp = _resolve_target_file_path(session_id, self.project_root, tc)
                            if tp:
                                target_paths[tc["id"]] = tp
                        except Exception:
                            pass

                def _on_before_file_mutation(fp: str) -> None:
                    if fp:
                        try:
                            self.file_history.record_checkpoint(
                                session_id, [fp], "Before tool execution"
                            )
                        except Exception:
                            pass

                def _on_after_file_mutation(fp: str) -> None:
                    if fp:
                        try:
                            self.file_history.record_checkpoint(
                                session_id, [fp], "After tool execution"
                            )
                        except Exception:
                            pass

                def _on_process_start(pid: int | str, cmd: str) -> None:
                    self._track_process_start(session_id, pid, cmd)

                def _on_process_exit(pid: int | str) -> None:
                    self._track_process_exit(session_id, pid)

                def _post_execute(
                    name: str, args: dict[str, Any], result: ToolResult, _ctx: Any
                ) -> ToolResult:
                    reminder = self._repeat_reminders.setdefault(session_id, RepeatToolReminder())
                    text, action = reminder.observe_with_action(name, args)
                    if text:
                        result.follow_up_messages.append(
                            ToolExecutionFollowUpMessage(role="system", content=text)
                        )
                    if action in ("r3", "stop"):
                        result.concludes_turn = True
                    if action == "stop":
                        result._force_stop_turn = True  # type: ignore[attr-defined]
                    return result

                is_plan = False
                try:
                    entry = self._get_entry(session_id) or {}
                    is_plan = bool(entry.get("planMode"))
                except Exception:
                    pass
                if not is_plan:
                    try:
                        state = self.get_session_state(session_id)
                        is_plan = bool(getattr(state, "plan_mode", False))
                    except Exception:
                        pass

                hooks = ToolExecutionHooks(
                    on_before_file_mutation=_on_before_file_mutation,
                    on_after_file_mutation=_on_after_file_mutation,
                    should_stop=lambda: self.is_interrupted(session_id),
                    on_process_start=_on_process_start,
                    on_process_exit=_on_process_exit,
                    on_load_skill=lambda skill_name: self.load_skill_by_name(
                        session_id, skill_name
                    ),
                    on_background_process_complete=lambda completion: (
                        self.add_background_process_completion_message(session_id, completion)
                    ),
                    permission_decision="allow",
                    post_execute=_post_execute,
                    sandbox_mode=(self.get_resolved_settings().get("permissions") or {}).get(
                        "sandbox"
                    ),
                    list_session_messages=lambda sid: self.list_session_messages(sid),
                    list_session_events=lambda sid: self.list_session_events(sid),
                    plan_mode=is_plan,
                    session_manager=self,
                    allowed_tools=self.get_resolved_settings().get("allowedTools"),
                )

                is_parallel = chunk_kind == "parallel" and len(chunk_tcs) > 1
                executions = await self.tool_executor.execute_tool_calls(
                    session_id, chunk_tcs, hooks=hooks, parallel=is_parallel
                )

                for execution in executions:
                    result = execution["result"]
                    exec_tc_id = execution["toolCallId"]
                    completed_tool_call_ids.add(exec_tc_id)

                    if result.get("awaitUserResponse") is True:
                        waiting = True
                    if (
                        result.get("concludesTurn") is True
                        or getattr(result, "concludes_turn", False) is True
                    ):
                        should_conclude_turn = True
                    if result.get("forceStopTurn") is True:
                        force_stop = True
                        should_conclude_turn = True

                    result_meta = result.get("metadata") if isinstance(result, dict) else None
                    if isinstance(result_meta, dict) and result_meta.get("exitPlanMode"):
                        self.set_plan_mode(session_id, False)
                    if isinstance(result_meta, dict) and result_meta.get("enterPlanMode"):
                        self.set_plan_mode(session_id, True)

                    tool_fn = self.message_converter.find_tool_function(tool_calls, exec_tc_id)
                    if not tool_fn:
                        matching = [t for t in chunk_tcs if t.get("id") == exec_tc_id]
                        if matching:
                            tool_fn = matching[0].get("function")

                    tool_msg = self._build_tool_message(
                        session_id,
                        exec_tc_id,
                        execution["content"],
                        tool_fn,
                        tool_meta=result_meta if isinstance(result_meta, dict) else None,
                    )
                    self._append_message(tool_msg)
                    self.on_assistant_message(tool_msg, True)

                    follow_up_messages: list[SessionMessage] = []
                    for follow_up in result.get("followUpMessages") or []:
                        role = (
                            follow_up.get("role", "user")
                            if isinstance(follow_up, dict)
                            else getattr(follow_up, "role", "user")
                        )
                        content = (
                            follow_up.get("content", "")
                            if isinstance(follow_up, dict)
                            else getattr(follow_up, "content", "")
                        )
                        content_params = (
                            follow_up.get("contentParams")
                            if isinstance(follow_up, dict)
                            else getattr(follow_up, "content_params", None)
                        )
                        if content:
                            follow_up_messages.append(
                                self._build_message(
                                    session_id,
                                    "user",
                                    content,
                                    meta={"contentParams": content_params, "advisoryRole": role}
                                    if content_params
                                    else {"advisoryRole": role},
                                )
                            )

                    for fum in follow_up_messages:
                        self._append_message(fum)

                    # Record post-mutation checkpoint for file-editing tools
                    tp = target_paths.get(exec_tc_id)
                    if tp:
                        try:
                            self.file_history.record_checkpoint(
                                session_id, [tp], "After tool execution"
                            )
                        except Exception:
                            pass
        finally:
            # Emit synthetic abort results for any unexecuted tool calls
            for tc in normalized_calls:
                tc_id = tc["id"]
                if tc_id not in completed_tool_call_ids:
                    tool_fn = tc.get("function")
                    tool_msg = self._build_tool_message(
                        session_id,
                        tc_id,
                        json.dumps(
                            {
                                "error": TOOL_ABORTED_BEFORE_DISPATCH,
                                "message": "Tool execution was aborted before dispatch.",
                            }
                        ),
                        tool_fn,
                    )
                    self._append_message(tool_msg)
                    self.on_assistant_message(tool_msg, True)
                    completed_tool_call_ids.add(tc_id)

        return ToolDispatchResult(
            waiting=waiting,
            stop_reason="tool_call_repeat" if force_stop else None,
        )

    async def _create_completion_with_retry(
        self, session_id: str, client: Any, request: dict[str, Any]
    ) -> dict[str, Any]:
        """Retry retryable LLM failures with automated multi-model failover cascade."""
        settings = self.get_resolved_settings()
        primary_model = str(request.get("model") or settings.get("model") or "gpt-6-luna")

        configured_fallbacks = (
            request.get("fallback_models")
            or settings.get("fallbackModels")
            or settings.get("fallback_models")
            or []
        )
        if isinstance(configured_fallbacks, str):
            configured_fallbacks = [m.strip() for m in configured_fallbacks.split(",") if m.strip()]

        fallback_chain: list[str] = [primary_model]
        for fb in configured_fallbacks:
            if isinstance(fb, str) and fb.strip() and fb.strip() not in fallback_chain:
                fallback_chain.append(fb.strip())

        attempted_models: list[str] = []
        fallback_reasons: list[str] = []
        last_error: Exception | None = None

        thinking_enabled = self.get_thinking_enabled()
        base_url = settings.get("baseURL")
        reasoning_effort = settings.get("reasoningEffort") or "max"

        def _build_request_for_model(target_model: str, req_base: dict[str, Any]) -> dict[str, Any]:
            req: dict[str, Any] = {
                "model": target_model,
                "messages": list(req_base.get("messages", [])),
            }
            if req_base.get("tools"):
                req["tools"] = req_base["tools"]
            if req_base.get("temperature") is not None:
                req["temperature"] = req_base["temperature"]
            eff_reasoning = self.get_reasoning_effort() or reasoning_effort
            req.update(
                build_thinking_request_options(
                    thinking_enabled,
                    base_url=base_url,
                    reasoning_effort=eff_reasoning,
                    model=target_model,
                    has_tools=bool(req.get("tools")),
                )
            )
            if not req.get("tools"):
                req.pop("tools", None)
            return req

        for model_idx, current_model in enumerate(fallback_chain):
            attempted_models.append(current_model)
            current_request = _build_request_for_model(current_model, request)

            retries_for_model = (
                DEFAULT_MAX_RETRIES if len(fallback_chain) == 1 else min(2, DEFAULT_MAX_RETRIES)
            )

            for attempt in range(retries_for_model + 1):
                if self.is_interrupted(session_id):
                    raise SessionInterrupted("Session was interrupted")
                try:
                    response = await self._create_completion(
                        client, current_request, session_id=session_id
                    )
                except Exception as err:
                    last_error = err
                    code = classify_llm_failure(err)
                    if code is None:
                        if model_idx < len(fallback_chain) - 1 and is_failover_eligible(err):
                            fallback_reasons.append(f"{current_model}: {err}")
                            break
                        raise

                    fallback_reasons.append(
                        f"{current_model} (attempt {attempt + 1}): {code} ({err})"
                    )
                    if code == "CONTEXT_OVERFLOW":
                        # Compact and retry on the current model, do not move to next model
                        try:
                            await self._compact_session(session_id, trigger="overflow")
                            from coderai.soul.session.log import derive_messages

                            refreshed_messages = self.list_session_messages(session_id)
                            current_request["messages"] = (
                                self.message_converter.convert_session_messages(
                                    derive_messages(refreshed_messages),
                                    current_model,
                                    thinking_enabled=thinking_enabled,
                                )
                            )
                            continue
                        except Exception as comp_err:
                            fallback_reasons.append(
                                f"{current_model} compaction failed: {comp_err}"
                            )
                            break
                    if attempt >= retries_for_model:
                        break

                    # Honor a provider Retry-After (seconds or HTTP-date); an
                    # over-cap value gives up in normal mode (harness rule).
                    provider_delay_ms = provider_retry_after_ms(err)
                    local_s = retry_delay_ms(attempt + 1) / 1000.0
                    if provider_delay_ms is not None:
                        provider_s = provider_delay_ms / 1000.0
                        if provider_s > DEFAULT_MAX_DELAY_MS / 1000.0:
                            break
                        delay_s = max(local_s, provider_s)
                    else:
                        delay_s = local_s
                    await asyncio.sleep(delay_s)
                    continue

                if is_empty_llm_response(response) and attempt < retries_for_model:
                    delay_s = retry_delay_ms(attempt + 1) / 1000.0
                    await asyncio.sleep(delay_s)
                    continue

                if not is_empty_llm_response(response):
                    if current_model != primary_model:
                        response["_fallback_info"] = {
                            "fallback_used": True,
                            "primary_model": primary_model,
                            "active_model": current_model,
                            "attempted_models": list(attempted_models),
                            "fallback_reasons": list(fallback_reasons),
                        }
                    return response

        if last_error is not None:
            raise last_error
        raise RuntimeError(
            "EMPTY_RESPONSE: model returned no content after retries and fallback cascade"
        )

    async def _create_completion(
        self,
        client: Any,
        request: dict[str, Any],
        *,
        emit_stream: bool = True,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        on_chunk = self.on_stream_chunk if emit_stream else None
        on_thinking = self.on_thinking_chunk if emit_stream else None
        is_cancelled = (lambda: self.is_interrupted(session_id)) if session_id else None
        result = await asyncio.to_thread(
            _call_stream_or_sync,
            client,
            request,
            on_chunk,
            self.on_llm_stream_progress,
            on_thinking,
            is_cancelled,
        )
        if self.get_resolved_settings().get("debugLogEnabled"):
            log_openai_chat_completion_debug(
                {
                    "projectRoot": self.project_root,
                    "model": request.get("model"),
                    "location": "SessionManager._create_completion",
                    "hasTools": bool(request.get("tools")),
                    "usage": result.get("usage"),
                }
            )
        return result

    async def _compact_session(
        self, session_id: str, trigger: str = "pressure", custom_instruction: str | None = None
    ) -> None:
        """Execute session compaction through the pluggable CompactionEngine."""
        # PreCompact fires first; deny aborts compaction.
        try:
            from coderai.hooks import run_pre_compact

            entry = self._get_entry(session_id) or {}
            _pre = run_pre_compact(
                session_id,
                self.project_root,
                trigger,
                int(entry.get("activeTokens", 0) or 0),
                self.get_resolved_settings(),
            )
            if _pre.decision == "deny":
                return
        except Exception:
            pass
        try:
            from coderai.wire.emitter import get_emitter

            get_emitter().compaction_begin()
        except Exception:
            pass
        try:
            res = await self.compaction_engine.compact_if_needed(session_id, trigger=trigger)
            if not res:
                # Forward custom_instruction if engine supports it
                try:
                    res = await self.compaction_engine.compact_now(
                        session_id, trigger=trigger, custom_instruction=custom_instruction
                    )  # type: ignore[call-arg]
                except TypeError:
                    res = await self.compaction_engine.compact_now(session_id, trigger=trigger)
        finally:
            try:
                from coderai.wire.emitter import get_emitter

                get_emitter().compaction_end()
            except Exception:
                pass
        if res:
            now = _now()
            self._update_entry(
                session_id,
                lambda e: {
                    **e,
                    "activeTokens": res.shadowed_token_count or e.get("activeTokens", 0),
                    "updateTime": now,
                },
            )
            # PostCompact fires after successful compaction.
            try:
                from coderai.hooks import run_post_compact

                run_post_compact(
                    session_id,
                    self.project_root,
                    trigger,
                    int(res.shadowed_token_count or 0),
                    self.get_resolved_settings(),
                )
            except Exception:
                pass
            try:
                soul = self.get_soul(session_id)
                if soul is not None and hasattr(soul, "notify_compacted"):
                    await soul.notify_compacted()
            except Exception:
                pass

    async def compact_session(
        self, session_id: str, trigger: str = "manual", custom_instruction: str | None = None
    ) -> None:
        """Public method to compact long session context history."""
        await self._compact_session(
            session_id, trigger=trigger, custom_instruction=custom_instruction
        )

    # ---- queries, delete, fork & undo ----

    def delete_session(self, session_id: str) -> bool:
        """Remove session messages and entry from index."""
        target_id = self.resolve_session_id(session_id) or session_id
        with self._seq_lock:
            self._checkpoint_counters.pop(target_id, None)
        index = self._load_index()
        initial_len = len(index.get("entries", []))
        index["entries"] = [e for e in index.get("entries", []) if e.get("id") != target_id]
        if len(index["entries"]) == initial_len:
            return False

        self._save_index(index)
        msg_file = self._messages_path(target_id)
        if msg_file.exists():
            try:
                msg_file.unlink()
            except Exception:
                pass

        clear_session_state(target_id)
        from coderai.background import get_task_supervisor

        get_task_supervisor().cleanup_session_tasks(target_id)
        ctrl = self.session_controllers.pop(target_id, None)
        if ctrl and not ctrl.is_set():
            ctrl.set()

        # Terminate live subprocesses and background jobs for this session
        try:
            self.kill_live_processes(target_id)
        except Exception:
            pass

        try:
            if hasattr(self, "job_store"):
                self.job_store.kill_all(target_id, reason=f"Session {target_id} deleted")
        except Exception:
            pass

        # Purge session spilled output files
        try:
            from coderai.spill import cleanup_spill_session

            cleanup_spill_session(target_id)
        except Exception:
            pass

        images_dir = self._storage()["project_dir"] / "images" / target_id
        if images_dir.exists():
            try:
                shutil.rmtree(images_dir)
            except Exception:
                pass
        with self._seq_lock:
            self._seq_counters.pop(target_id, None)
            self._turn_counters.pop(target_id, None)
            self._step_counters.pop(target_id, None)
            self._turn_step_synced.discard(target_id)
            self._messages_cache.pop(target_id, None)
        return True

    def rename_session(self, session_id: str, new_title: str) -> bool:
        """Rename an existing session title/summary in index and memory.

        Manual titles are capped at 200 chars and set
        ``title_locked`` so auto-generation never overwrites them.
        """
        cleaned_title = (new_title or "").strip()[:200]
        if not cleaned_title:
            return False
        target_id = self.resolve_session_id(session_id) or session_id
        entry = self._get_entry(target_id)
        if not entry:
            return False
        self._update_entry(
            target_id,
            lambda e: {
                **e,
                "summary": cleaned_title,
                "title_locked": True,
                "updateTime": _now(),
            },
        )
        # Phase 2: mirror manual titles into persisted state (custom_title
        # + title_generated guard against auto-generation overwrites).
        try:
            state = self.get_session_state(target_id)
            state.custom_title = cleaned_title
            state.title_generated = True
            self._save_session_state(target_id)
        except Exception:
            pass
        return True

    def fork_session(
        self,
        source_session_id: str,
        at_message_id_or_seq: str | int | None = None,
    ) -> str | None:
        """Fork an existing session into a new independent session branch with cloned message history, event logs, and file checkpoint."""
        return _fork_session_fn(self, source_session_id, at_message_id_or_seq)

    def list_undo_targets(self, session_id: str) -> list[dict[str, Any]]:
        """Return all undoable user turns with checkpoint hashes and prompt previews in chronological order."""
        return _list_undo_targets_fn(self, session_id)

    def undo(
        self,
        session_id: str,
        target_message_id: str | None = None,
        mode: str = "restore_both",
    ) -> bool:
        """Revert files and/or message history back to a previous user prompt checkpoint.

        Modes:
          - "restore_both" (default): Reverts disk files and truncates message history.
          - "restore_conversation_only": Truncates message history without modifying disk files.
          - "restore_code_only": Reverts disk files without truncating message history.
        """
        return _undo_fn(self, session_id, target_message_id, mode)

    def list_sessions(self) -> list[SessionEntry]:
        return [_entry_from_dict(e) for e in self._load_index()["entries"]]

    def get_session(self, session_id: str) -> SessionEntry | None:
        entry = self._get_entry(session_id)
        return _entry_from_dict(entry) if entry else None

    async def init_mcp_servers(self) -> None:
        # A background load already in flight wins (deferred-loading parity).
        pending = getattr(self, "_mcp_load_task", None)
        try:
            running = pending is not None and not pending.done()
        except Exception:
            running = False
        if running:
            import asyncio as _asyncio

            if pending is not _asyncio.current_task():
                try:
                    await pending
                except Exception:
                    pass
            return
        await self.mcp_manager.initialize(self.get_resolved_settings().get("mcpServers"))
        self._refresh_mcp_tool_definitions()

    async def sync_mcp_servers(self) -> None:
        """Sync MCP servers with current resolved settings at runtime."""
        settings = self.get_resolved_settings()
        await self.mcp_manager.sync_servers(settings.get("mcpServers"))
        self._refresh_mcp_tool_definitions()

    def _refresh_mcp_tool_definitions(self) -> None:
        self.mcp_tool_definitions = self.mcp_manager.get_mcp_tool_definitions()

    def refresh_plugin_tools(self) -> None:
        """Reload plugin definitions + refresh injected configs."""
        try:
            self.tool_executor.refresh_plugin_tools()
        except Exception:
            pass
        try:
            from coderai.plugin.manager import (
                collect_host_values,
                get_plugins_dir,
                refresh_plugin_configs,
            )

            plugins_dir = get_plugins_dir()
            if not plugins_dir.is_dir():
                # No plugins installed: config re-injection is a no-op, so
                # skip the full client construction (settings resolve +
                # typed-config load + OpenAI client) on the startup path.
                return
            merged = dict(self.get_resolved_settings())
            try:
                info = self.create_openai_client() or {}
                if isinstance(info, dict):
                    merged.update(info)
            except Exception:
                pass
            refresh_plugin_configs(plugins_dir, collect_host_values(merged))
        except Exception:
            pass

    def get_external_tool_definitions(
        self, tools_preset: str | None = None
    ) -> list[dict[str, Any]] | None:
        """MCP + plugin tool definitions for the model (None under a tool preset)."""
        if tools_preset:
            return None
        defs = list(self.mcp_tool_definitions or [])
        try:
            defs.extend(self.tool_executor.plugin_tool_definitions())
        except Exception:
            pass
        return defs

    def start_background_mcp_loading(self) -> None:
        """Connect MCP servers in the background for fast shell start.

        The first turn joins the load via ``_await_mcp_ready``; non-interactive
        callers keep awaiting :meth:`init_mcp_servers` inline instead.
        """
        if self.mcp_manager.initialized:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        pending = getattr(self, "_mcp_load_task", None)
        try:
            if pending is not None and not pending.done():
                return
        except Exception:
            pass
        self._mcp_load_task = _track_background_task(
            loop.create_task(self._background_mcp_load()), "background_mcp_load"
        )

    async def _background_mcp_load(self) -> None:
        try:
            from coderai.wire.emitter import get_emitter

            get_emitter().mcp_loading_begin()
        except Exception:
            pass
        try:
            await self.mcp_manager.initialize(self.get_resolved_settings().get("mcpServers"))
            self._refresh_mcp_tool_definitions()
        except Exception:
            pass
        finally:
            try:
                from coderai.wire.emitter import get_emitter

                get_emitter().mcp_loading_end()
                self.emit_mcp_status(loading=False)
            except Exception:
                pass

    async def _await_mcp_ready(self) -> None:
        pending = getattr(self, "_mcp_load_task", None)
        if pending is None:
            return
        try:
            await pending
        except Exception:
            pass

    def emit_mcp_status(self, loading: bool = False) -> None:
        """Publish a wire ``StatusUpdate`` with the current MCP snapshot."""
        try:
            from coderai.wire.emitter import get_emitter
            from coderai.wire.types import (
                MCPServerSnapshot,
                MCPStatusSnapshot,
                StatusUpdate,
            )

            _snapshot_states = {
                "ready": "connected",
                "starting": "connecting",
                "reconnecting": "connecting",
                "failed": "failed",
                "unauthorized": "unauthorized",
                "disabled": "pending",
            }
            statuses = self.mcp_manager.get_status()
            servers = tuple(
                MCPServerSnapshot(
                    name=s.name,
                    status=_snapshot_states.get(s.status, "pending"),  # type: ignore[arg-type]
                    tools=tuple(s.tools),
                )
                for s in statuses
            )
            get_emitter().send(
                StatusUpdate(
                    mcp_status=MCPStatusSnapshot(
                        loading=loading,
                        connected=sum(1 for s in statuses if s.connected),
                        total=len(statuses),
                        tools=len(self.mcp_manager.tools),
                        servers=servers,
                    )
                )
            )
        except Exception:
            pass

    def dispose(self) -> None:
        """Best-effort sync dispose. Prefer ``close_session_manager`` from an async context."""
        try:
            unregister_session_manager(self)
        except Exception:
            pass
        for event in self.session_controllers.values():
            try:
                event.set()
            except Exception:
                pass
        try:
            self.kill_live_processes()
        except Exception:
            pass
        try:
            if hasattr(self, "job_store"):
                self.job_store.kill_all(reason="SessionManager disposed")
        except Exception:
            pass
        try:
            from coderai.terminal.manager import get_terminal_manager

            get_terminal_manager().close_all()
        except Exception:
            pass
        try:
            from coderai.sandbox import cleanup_seatbelt_profiles

            cleanup_seatbelt_profiles()
        except Exception:
            pass
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            try:
                asyncio.run(self.mcp_manager.disconnect())
            except Exception:
                pass
            return
        # A running loop already owns async teardown via close_session_manager.


def _entry_from_dict(d: dict[str, Any]) -> SessionEntry:
    return _entry_from_dict_fn(d)
