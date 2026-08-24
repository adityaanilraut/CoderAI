"""Session lifecycle and public APIs for the CoderAI runtime.

Persistence: append-only JSONL event log + a sessions index under
`~/.coderai/projects/<projectCode>/`, with token-threshold compaction,
isolated GitFileHistory checkpoint-based undo, subagent orchestration, and Plan Mode gating.

Event model: ``SessionEvent`` from ``coderai.core.events`` is the canonical
log entry.  Legacy ``SessionMessage`` is preserved for backward compat with
existing session files and CLI rendering.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import pathlib
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from typing import Any
from collections.abc import Callable

from coderai.core.common.debug_logger import log_openai_chat_completion_debug
from coderai.core.common.file_history import GitFileHistory
from coderai.core.common.file_utils import read_text_file_tail
from coderai.core.common.llm_retry import (
    DEFAULT_MAX_RETRIES,
    classify_llm_failure,
    is_empty_llm_response,
    retry_delay_ms,
)
from coderai.core.common.message_converter import OpenAIMessageConverter
from coderai.core.common.repeat_tool_reminder import RepeatToolReminder
from coderai.core.common.usage import accumulate_usage_dict, extract_usage_dict
from coderai.core.common.validate import repair_json_string
from coderai.core.mcp import McpManager
from coderai.core.permissions import (
    PermissionTicket,
    build_permission_tool_execution,
    resolve_snippet_file_path,
)
from coderai.core.compaction import (
    BasicCompaction,
    DEFAULT_MAX_TOOL_RESULT_CHARS,
    ToolResultPruner,
)
from coderai.core.prompt import (
    get_init_command_prompt,
    get_plan_mode_prompt,
    get_runtime_context,
    get_system_prompt,
    load_agent_instructions,
)
from coderai.core.skill import (
    build_skill_documents_prompt,
    list_skills,
    load_skill,
    match_skills_for_prompt,
)
from coderai.core.events import (
    SessionEvent,
)
from coderai.core.session_store import JsonlSessionStore
from coderai.core.session_store import get_project_code as get_project_code
from coderai.core.state import clear_session_state, rebuild_session_state_from_history
from coderai.core.tools.executor import ToolExecutor
from coderai.core.tools.types import (
    BackgroundProcessCompletion,
    ToolExecutionFollowUpMessage,
    ToolExecutionHooks,
    ToolResult,
)

MAX_ITERATIONS = 80_000
MAX_SESSION_ENTRIES = 50
BACKGROUND_FAILURE_LOG_TAIL_CHARS = 4000


def sanitize_repetition_loops(text: str) -> str:
    """Detect and collapse pathological token repetition loops in model output."""
    if not text or len(text) < 40:
        return text
    pattern = re.compile(r"(.{6,150}?)(?:\s*\1){3,}", re.DOTALL)

    def _replace(match: re.Match) -> str:
        unit = match.group(1).strip()
        return f"{unit} [truncated repetition loop]"

    return pattern.sub(_replace, text)


@dataclass
class SessionMessage:
    id: str
    session_id: str
    role: str  # system | user | assistant | tool
    content: str = ""
    tool_calls: list[Any] | None = None
    tool_call_id: str | None = None
    thinking: str | None = None
    compacted: bool = False
    visible: bool = True
    create_time: str = ""
    update_time: str = ""
    meta: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sessionId": self.session_id,
            "role": self.role,
            "content": self.content,
            "toolCalls": self.tool_calls,
            "toolCallId": self.tool_call_id,
            "thinking": self.thinking,
            "compacted": self.compacted,
            "visible": self.visible,
            "createTime": self.create_time,
            "updateTime": self.update_time,
            "meta": self.meta,
        }


@dataclass
class SessionEntry:
    id: str
    summary: str = ""
    assistant_reply: str | None = None
    assistant_thinking: str | None = None
    assistant_refusal: str | None = None
    tool_calls: list[Any] | None = None
    status: str = "pending"
    fail_reason: str | None = None
    ask_permissions: list[dict[str, Any]] | None = None
    processes: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    usage_per_model: dict[str, Any] | None = None
    active_tokens: int = 0
    create_time: str = ""
    update_time: str = ""
    plan_mode: bool = False
    fork_of: str | None = None
    parent_session_id: str | None = None
    fork_point: str | int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "summary": self.summary,
            "assistantReply": self.assistant_reply,
            "assistantThinking": self.assistant_thinking,
            "assistantRefusal": self.assistant_refusal,
            "toolCalls": self.tool_calls,
            "status": self.status,
            "failReason": self.fail_reason,
            "askPermissions": self.ask_permissions,
            "processes": self.processes,
            "usage": self.usage,
            "usagePerModel": self.usage_per_model,
            "activeTokens": self.active_tokens,
            "createTime": self.create_time,
            "updateTime": self.update_time,
            "planMode": self.plan_mode,
            "forkOf": self.fork_of,
            "parentSessionId": self.parent_session_id or self.fork_of,
            "forkPoint": self.fork_point,
        }


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


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
        self.tool_executor = ToolExecutor(
            self.project_root, create_openai_client, mcp_manager=self.mcp_manager
        )
        self.mcp_tool_definitions: list[dict[str, Any]] = []
        self.message_converter = OpenAIMessageConverter(
            render_init_prompt=lambda: get_init_command_prompt(self.project_root)
        )
        self._active_session_id: str | None = None
        self._override_model: str | None = None
        self._override_reasoning_effort: str | None = None
        self.session_controllers: dict[str, asyncio.Event] = {}
        self.compaction_engine = BasicCompaction(self)
        self.session_store = JsonlSessionStore(self.project_root, max_entries=MAX_SESSION_ENTRIES)
        self.file_history = GitFileHistory(
            self.project_root, str(self._storage()["project_dir"] / "file-history" / ".git")
        )
        self._repeat_reminders: dict[str, RepeatToolReminder] = {}
        # Subsystems: Jobs, Schedule, Agents
        from coderai.core.jobs import JobStore
        from coderai.core.schedule import ScheduleManager
        from coderai.core.agents import AgentRegistry

        self.job_store = JobStore()
        sched_storage = str(self._storage()["project_dir"] / "schedule.json")
        self.schedule_manager = ScheduleManager(sched_storage)
        self.agent_registry = AgentRegistry()

        # Event-model state: per-session turn/step/seq counters
        self._turn_counters: dict[str, int] = {}
        self._step_counters: dict[str, int] = {}
        self._seq_counters: dict[str, int] = {}

    def set_model(self, model_name: str) -> None:
        self._override_model = model_name.strip() if model_name else None

    def get_active_model(self) -> str:
        if self._override_model:
            return self._override_model
        return str(self.get_resolved_settings().get("model") or "gpt-5.6-luna")

    def set_reasoning_effort(self, effort: str) -> None:
        from coderai.core.common.openai_thinking import normalize_reasoning_effort

        norm = normalize_reasoning_effort(effort)
        self._override_reasoning_effort = norm

    def get_reasoning_effort(self) -> str:
        if self._override_reasoning_effort:
            return self._override_reasoning_effort
        return str(self.get_resolved_settings().get("reasoningEffort") or "max")

    def get_diff(self, session_id: str | None = None, from_checkpoint: str | None = None) -> str:
        sid = session_id or self._active_session_id
        if not sid:
            return ""
        return self.file_history.get_diff(sid, from_checkpoint=from_checkpoint)

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
        from coderai.core.permissions import get_permission_ticket_registry

        sid = session_id or self._active_session_id or ""
        return get_permission_ticket_registry().request_escalation(
            session_id=sid,
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
        from coderai.core.permissions import get_permission_ticket_registry

        sid = session_id or self._active_session_id or ""
        return get_permission_ticket_registry().list_active_tickets(session_id=sid)

    def revoke_permission_ticket(self, ticket_id: str) -> bool:
        """Revoke a capability escalation ticket by ID."""
        from coderai.core.permissions import get_permission_ticket_registry

        return get_permission_ticket_registry().revoke_ticket(ticket_id)

    # ---- storage ----

    def _storage(self) -> dict[str, pathlib.Path]:
        return self.session_store.storage_paths()

    def _messages_path(self, session_id: str) -> pathlib.Path:
        return self.session_store.messages_path(session_id)

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

    def _next_seq(self, session_id: str) -> int:
        """Return and increment the monotonic event sequence for a session."""
        seq = self._seq_counters.get(session_id, 0)
        self._seq_counters[session_id] = seq + 1
        return seq

    def _current_turn(self, session_id: str) -> int:
        return self._turn_counters.get(session_id, 0)

    def _current_step(self, session_id: str) -> int:
        return self._step_counters.get(session_id, 0)

    def _append_event(self, session_id: str, event: SessionEvent) -> None:
        """Append a typed SessionEvent to the JSONL log.

        Events are written in the new format alongside legacy messages.
        The JSONL line includes a ``type`` key that distinguishes it from
        legacy ``SessionMessage`` dicts (which have ``role`` instead).
        """
        self.session_store.append_row(session_id, event.to_dict())

    def _save_messages(self, session_id: str, messages: list[SessionMessage]) -> None:
        self.session_store.replace_rows(
            session_id, [self._serialize_message(message) for message in messages]
        )

    def list_session_messages(self, session_id: str) -> list[SessionMessage]:
        messages: list[SessionMessage] = []
        max_seq = self._seq_counters.get(session_id, 0)
        for row in self.session_store.read_rows(session_id):
            if "seq" in row and isinstance(row["seq"], int):
                max_seq = max(max_seq, row["seq"] + 1)
            message = self._deserialize_message(row, session_id)
            if message is not None:
                messages.append(message)
        self._seq_counters[session_id] = max_seq
        return messages

    def list_session_events(self, session_id: str) -> list[SessionEvent]:
        return self.session_store.list_events(session_id)

    def _serialize_message(self, m: SessionMessage) -> dict[str, Any]:
        ts = 0
        if m.create_time:
            try:
                ts = int(datetime.datetime.fromisoformat(m.create_time).timestamp() * 1000)
            except Exception:
                ts = int(time.time() * 1000)
        else:
            ts = int(time.time() * 1000)
        return {
            "id": m.id,
            "sessionId": m.session_id,
            "role": m.role,
            "content": m.content,
            "toolCalls": m.tool_calls,
            "toolCallId": m.tool_call_id,
            "thinking": m.thinking,
            "compacted": m.compacted,
            "visible": m.visible,
            "createTime": m.create_time,
            "updateTime": m.update_time,
            "timestamp": ts,
            "meta": m.meta,
        }

    def _deserialize_message(self, d: dict[str, Any], session_id: str) -> SessionMessage | None:
        from coderai.core.events import (
            LOG_ONLY_EVENT_TYPES,
            USER_MESSAGE,
            ASSISTANT_MESSAGE,
            TOOL_RESULT,
            COMPACTION_SUMMARY,
            STEERING_MESSAGE,
        )

        event_type = d.get("type")
        if event_type:
            if event_type in LOG_ONLY_EVENT_TYPES:
                return None
            data = d.get("data") or {}
            time_val = d.get("time") or d.get("timestamp") or 0.0
            create_time = ""
            if time_val:
                try:
                    create_time = datetime.datetime.fromtimestamp(
                        float(time_val) / 1000.0, tz=datetime.timezone.utc
                    ).isoformat()
                except Exception:
                    create_time = _now()
            if event_type == USER_MESSAGE:
                return SessionMessage(
                    id=data.get("id") or uuid.uuid4().hex,
                    session_id=session_id,
                    role="user" if data.get("source") != "system" else "system",
                    content=data.get("content") or "",
                    create_time=create_time or _now(),
                    update_time=create_time or _now(),
                    meta=data.get("meta"),
                )
            elif event_type == ASSISTANT_MESSAGE:
                return SessionMessage(
                    id=data.get("id") or uuid.uuid4().hex,
                    session_id=session_id,
                    role="assistant",
                    content=data.get("content") or "",
                    tool_calls=data.get("toolCalls"),
                    thinking=data.get("thinking"),
                    create_time=create_time or _now(),
                    update_time=create_time or _now(),
                    meta=data.get("meta"),
                )
            elif event_type == TOOL_RESULT:
                return SessionMessage(
                    id=uuid.uuid4().hex,
                    session_id=session_id,
                    role="tool",
                    content=data.get("content") or "",
                    tool_call_id=data.get("callId"),
                    create_time=create_time or _now(),
                    update_time=create_time or _now(),
                    meta=data.get("meta"),
                )
            elif event_type == COMPACTION_SUMMARY:
                return SessionMessage(
                    id=uuid.uuid4().hex,
                    session_id=session_id,
                    role="system",
                    content=f"There are earlier parts of the conversation. Here is a summary:\n\n{data.get('content', '')}",
                    create_time=create_time or _now(),
                    update_time=create_time or _now(),
                    meta={
                        "isSummary": True,
                        "kind": "compact/summary",
                        "replacedIds": data.get("shadowedIds", []),
                    },
                    visible=False,
                )
            elif event_type == STEERING_MESSAGE:
                return SessionMessage(
                    id=data.get("id") or uuid.uuid4().hex,
                    session_id=session_id,
                    role="user",
                    content=data.get("content") or "",
                    create_time=create_time or _now(),
                    update_time=create_time or _now(),
                    meta=data.get("meta"),
                )
            return None

        # Legacy SessionMessage dict
        create_time = d.get("createTime") or ""
        if not create_time and d.get("timestamp"):
            try:
                create_time = datetime.datetime.fromtimestamp(
                    float(d["timestamp"]) / 1000.0, tz=datetime.timezone.utc
                ).isoformat()
            except Exception:
                create_time = _now()
        return SessionMessage(
            id=d.get("id") or uuid.uuid4().hex,
            session_id=session_id,
            role=d.get("role") or "user",
            content=d.get("content") or "",
            tool_calls=d.get("toolCalls"),
            tool_call_id=d.get("toolCallId"),
            thinking=d.get("thinking"),
            compacted=bool(d.get("compacted")),
            visible=d.get("visible") is not False,
            create_time=create_time or _now(),
            update_time=d.get("updateTime") or create_time or _now(),
            meta=d.get("meta"),
        )

    def _update_entry(
        self, session_id: str, mutate: Callable[[dict[str, Any]], dict[str, Any]]
    ) -> bool:
        index = self._load_index()
        for i, entry in enumerate(index["entries"]):
            if entry.get("id") == session_id:
                index["entries"][i] = mutate(entry)
                self._save_index(index)
                if self.on_session_entry_updated:
                    self.on_session_entry_updated(_entry_from_dict(index["entries"][i]))
                return True
        return False

    def _get_entry(self, session_id: str) -> dict[str, Any] | None:
        for entry in self._load_index()["entries"]:
            if entry.get("id") == session_id:
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
        """Interrupt and cancel a running session."""
        ctrl = self.session_controllers.get(session_id)
        if ctrl:
            ctrl.set()
        else:
            event = asyncio.Event()
            event.set()
            self.session_controllers[session_id] = event
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
        """Read and format trailing failure log slice for background process diagnostics."""
        if not output_path:
            return None
        tail = read_text_file_tail(output_path, max_chars=BACKGROUND_FAILURE_LOG_TAIL_CHARS)
        if not tail or not tail.get("content"):
            return None
        prefix = (
            f"... (last {len(tail['content'])} of {tail['total_bytes']} bytes)\n"
            if tail.get("truncated")
            else ""
        )
        return (
            f'<background_task_failure_log path="{output_path}">\n'
            f"{prefix}{tail['content']}\n"
            "</background_task_failure_log>"
        )

    def _dispatch_due_schedules(self, session_id: str) -> bool:
        """Check for due timers and inject reminder messages into the session."""
        try:
            from coderai.core.schedule import get_schedule_manager

            mgr = get_schedule_manager()
            due_records = mgr.check_due(session_id=session_id)
            if not due_records:
                return False

            for rec in due_records:
                reminder_text = (
                    f'<scheduled_reminder id="{rec.id}" kind="{rec.kind}">\n'
                    f"Prompt: {rec.prompt}\n"
                    f"Scheduled at: {rec.scheduled_at}\n"
                    f"</scheduled_reminder>"
                )
                msg = self._build_message(session_id, "user", reminder_text)
                self._append_message(msg)
                if self.on_user_message:
                    self.on_user_message(msg)
            return True
        except Exception:
            return False

    def add_background_process_completion_message(
        self, session_id: str, completion: BackgroundProcessCompletion
    ) -> None:
        """Append completion or failure notification message with log tail slice to session."""
        status = "completed" if completion.ok else "failed"
        exit_text = (
            f"exit code {completion.exit_code}"
            if completion.exit_code is not None
            else (
                f"signal {completion.signal}"
                if completion.signal
                else "exit code 0"
                if completion.ok
                else "unknown exit status"
            )
        )
        duration_s = max(0, completion.completed_at_ms - completion.started_at_ms) / 1000.0
        duration_text = (
            f"{duration_s:.1f}s"
            if duration_s < 60
            else f"{int(duration_s // 60)}m {int(duration_s % 60)}s"
        )

        base_content = (
            f'Background command "{completion.command}" (pid {completion.process_id}) '
            f"{status} with {exit_text} after {duration_text}."
        )
        log_tail = (
            None
            if completion.ok
            else self.build_background_failure_log_tail_slice(completion.output_path)
        )
        content = f"{base_content}\n{log_tail}" if log_tail else base_content

        msg = self._build_message(
            session_id,
            "system",
            content,
            meta={
                "isBackgroundCompletion": True,
                "taskId": completion.task_id,
                "processId": completion.process_id,
                "exitCode": completion.exit_code,
                "signal": completion.signal,
                "ok": completion.ok,
                "outputPath": completion.output_path,
            },
        )
        self._append_message(msg)

    def _track_process_start(self, session_id: str, pid: int | str, command: str) -> None:
        pid_key = str(pid)
        self._update_entry(
            session_id,
            lambda e: {
                **e,
                "processes": {
                    **(e.get("processes") or {}),
                    pid_key: {
                        "pid": pid,
                        "command": command,
                        "startedAt": _now(),
                    },
                },
            },
        )

    def _track_process_exit(self, session_id: str, pid: int | str) -> None:
        pid_key = str(pid)

        def mutate(e: dict[str, Any]) -> dict[str, Any]:
            procs = dict(e.get("processes") or {})
            procs.pop(pid_key, None)
            return {**e, "processes": procs}

        self._update_entry(session_id, mutate)

    def kill_live_processes(self, session_id: str | None = None) -> None:
        """Kill all tracked live processes for a session."""
        from coderai.core.tools.bash import kill_process_tree

        if not session_id:
            return
        entry = self._get_entry(session_id)
        if entry and entry.get("processes"):
            for pid_str in list(entry["processes"].keys()):
                try:
                    pid = int(pid_str)
                    kill_process_tree(pid)
                except Exception:
                    pass
            self._update_entry(
                session_id,
                lambda e: {**e, "processes": {}, "updateTime": _now()},
            )

    def maybe_notify_task_completion(self, session_id: str, started_at_ms: int) -> None:
        """Trigger configured notification command when session finishes."""
        from coderai.core.common.notify import launch_notify_script

        settings = self.get_resolved_settings()
        notify_command = settings.get("notify")
        if not notify_command:
            return

        entry = self._get_entry(session_id)
        status = entry.get("status", "completed") if entry else "completed"
        fail_reason = entry.get("failReason") if entry else None
        duration_ms = max(0, int(time.time() * 1000) - started_at_ms)

        messages = self.list_session_messages(session_id)
        last_assistant = next(
            (m for m in reversed(messages) if m.role == "assistant" and m.content), None
        )
        fallback_summary = entry.get("summary") if entry else "Task finished"
        body = (
            (last_assistant.content if last_assistant else fallback_summary) or "Task finished"
        )[:200]

        launch_notify_script(
            notify_command,
            duration_ms=duration_ms,
            working_directory=self.project_root,
            context={
                "status": status,
                "failReason": fail_reason or "",
                "body": body,
                "title": (
                    f"CoderAI: {(entry.get('summary') or 'Task')[:50]}"
                    if entry
                    else "CoderAI: Task"
                ),
            },
        )

    def _create_empty_session(self, plan_mode: bool = False) -> str:
        session_id = uuid.uuid4().hex
        now = _now()
        index = self._load_index()
        entry: dict[str, Any] = {
            "id": session_id,
            "summary": "New Session",
            "assistantReply": None,
            "assistantThinking": None,
            "assistantRefusal": None,
            "toolCalls": None,
            "status": "ready",
            "failReason": None,
            "usage": None,
            "usagePerModel": None,
            "activeTokens": 0,
            "processes": {},
            "createTime": now,
            "updateTime": now,
            "planMode": plan_mode,
        }
        index["entries"].append(entry)
        index["entries"] = sorted(
            index["entries"], key=lambda e: e.get("updateTime", ""), reverse=True
        )[:MAX_SESSION_ENTRIES]
        self._save_index(index)
        self.file_history.ensure_session(session_id)
        self._active_session_id = session_id
        return session_id

    async def respond_permissions(
        self, session_id: str, replies: list[dict[str, Any]], plan_mode: bool | None = None
    ) -> None:
        """Respond to pending permission requests in a session."""
        await self.reply_session(session_id, permission_replies=replies, plan_mode=plan_mode)

    async def create_session(
        self,
        user_prompt: str,
        plan_mode: bool = False,
        skills: list[str] | None = None,
    ) -> str:
        session_id = uuid.uuid4().hex
        summary = (user_prompt or "[Image Prompt]")[:100]
        now = _now()
        index = self._load_index()
        entry: dict[str, Any] = {
            "id": session_id,
            "summary": summary,
            "assistantReply": None,
            "assistantThinking": None,
            "assistantRefusal": None,
            "toolCalls": None,
            "status": "pending",
            "failReason": None,
            "usage": None,
            "usagePerModel": None,
            "activeTokens": 0,
            "processes": {},
            "createTime": now,
            "updateTime": now,
            "planMode": plan_mode,
        }
        index["entries"].append(entry)
        index["entries"] = sorted(
            index["entries"], key=lambda e: e.get("updateTime", ""), reverse=True
        )[:MAX_SESSION_ENTRIES]
        self._save_index(index)

        # File history session checkpoint
        self.file_history.ensure_session(session_id)
        ckpt_res = self.file_history.record_tracked_files_checkpoint(
            session_id, "User prompt checkpoint"
        )

        model = self.get_active_model()
        settings = self.get_resolved_settings()
        sandbox_mode = (settings.get("permissions") or {}).get("sandbox")
        prompt_options = {
            "model": model,
            "nonInteractive": self.non_interactive,
            "sandboxMode": sandbox_mode,
            "workspaceRoot": self.project_root,
            "preset": settings.get("preset") or settings.get("toolsPreset"),
            "enabledSkills": settings.get("enabledSkills"),
            "skillScanPaths": settings.get("skillScanPaths"),
        }
        self._append_message(
            self._build_message(session_id, "system", get_system_prompt(prompt_options))
        )
        instructions = load_agent_instructions(self.project_root)
        if instructions:
            self._append_message(self._build_message(session_id, "system", instructions))
        if plan_mode:
            self._append_message(
                self._build_message(
                    session_id,
                    "system",
                    get_plan_mode_prompt(),
                    meta={"isPlanMode": True},
                )
            )

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
                    "checkpointHash": ckpt_res.checkpoint_hash,
                    "userPrompt": {"planMode": plan_mode},
                    "rawPrompt": user_prompt,
                },
            )
        )
        await self._inject_matched_skills(session_id, user_prompt, skills)
        self._active_session_id = session_id
        await self._activate(session_id)
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
    ) -> None:
        entry = self._get_entry(session_id)
        if not entry:
            await self.create_session(user_prompt or "", plan_mode=bool(plan_mode), skills=skills)
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

        # Handle /continue without appending redundant user message
        is_continue = self.is_continue_prompt(user_prompt)

        if permission_replies is not None:
            # If user provided a message alongside permission replies, queue it as deferred prompt
            deferred_prompt = user_prompt if (user_prompt and not is_continue) else None
            await self._activate(
                session_id, permission_replies=permission_replies, deferred_prompt=deferred_prompt
            )
            return

        if user_prompt and not is_continue:
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
                    user_prompt,
                    meta={
                        "checkpointHash": ckpt_res.checkpoint_hash,
                        "userPrompt": {"planMode": curr_mode},
                    },
                )
            )
            await self._inject_matched_skills(session_id, user_prompt, skills)
        elif skills:
            self._append_skill_messages(session_id, skills)

        self._active_session_id = session_id
        await self._activate(session_id)

    def list_available_skills(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Discover skills with enabledSkills filtering, custom scan paths, and loaded flags."""
        settings = self.get_resolved_settings()
        enabled = settings.get("enabledSkills") or {}
        custom_paths = settings.get("skillScanPaths") or []
        skills = list_skills(
            self.project_root, enabled_skills=enabled, custom_scan_paths=custom_paths
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
        from coderai.core.agent_loop import AgentLoop

        await AgentLoop(self, session_id).run(
            permission_replies=permission_replies,
            deferred_prompt=deferred_prompt,
        )

    async def _append_tool_messages(
        self,
        session_id: str,
        tool_calls: list[Any],
        permission_replies: list[dict[str, Any]] | None = None,
        message_permissions: list[dict[str, Any]] | None = None,
    ) -> bool:
        waiting = False
        ctrl = self.session_controllers.get(session_id)
        if not tool_calls:
            return waiting

        read_only_tool_names = {
            "read",
            "Read",
            "grep",
            "Grep",
            "glob",
            "Glob",
            "WebSearch",
            "web_search",
            "WebFetch",
            "web_fetch",
            "lsp",
            "session_query",
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

        # Partition into contiguous execution chunks (parallel read-only vs sequential mutating)
        chunks: list[tuple[str, list[dict[str, Any]]]] = []
        current_chunk_kind: str | None = None
        current_chunk: list[dict[str, Any]] = []

        for tc in normalized_calls:
            fn = tc.get("function") or {}
            fn_name = str(fn.get("name", "") if isinstance(fn, dict) else "")
            blocked = build_permission_tool_execution(tc, permission_replies, message_permissions)

            kind = (
                "blocked"
                if blocked
                else "parallel"
                if fn_name in read_only_tool_names
                else "sequential"
            )
            if current_chunk_kind is None or current_chunk_kind == kind:
                current_chunk_kind = kind
                current_chunk.append(tc)
            else:
                chunks.append((current_chunk_kind, current_chunk))
                current_chunk_kind = kind
                current_chunk = [tc]

        if current_chunk and current_chunk_kind is not None:
            chunks.append((current_chunk_kind, current_chunk))

        for chunk_kind, chunk_tcs in chunks:
            if ctrl and ctrl.is_set():
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
                continue

            # Checkpoints for mutating calls
            target_paths: dict[str, str] = {}
            if chunk_kind == "sequential":
                for tc in chunk_tcs:
                    func = tc.get("function") if isinstance(tc, dict) else None
                    name = str(func.get("name", "")) if isinstance(func, dict) else ""
                    if name in ("edit", "Edit", "write", "Write"):
                        try:
                            tp = _resolve_target_file_path(session_id, self.project_root, tc)
                            if tp:
                                target_paths[tc["id"]] = tp
                                self.file_history.record_checkpoint(
                                    session_id, [tp], f"Before {name} tool execution"
                                )
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
                text = reminder.observe(name, args)
                if text:
                    result.follow_up_messages.append(
                        ToolExecutionFollowUpMessage(role="system", content=text)
                    )
                return result

            hooks = ToolExecutionHooks(
                on_before_file_mutation=_on_before_file_mutation,
                on_after_file_mutation=_on_after_file_mutation,
                should_stop=lambda: self.is_interrupted(session_id),
                on_process_start=_on_process_start,
                on_process_exit=_on_process_exit,
                on_load_skill=lambda skill_name: self.load_skill_by_name(session_id, skill_name),
                on_background_process_complete=lambda completion: (
                    self.add_background_process_completion_message(session_id, completion)
                ),
                permission_decision="allow",
                post_execute=_post_execute,
                sandbox_mode=(self.get_resolved_settings().get("permissions") or {}).get("sandbox"),
                list_session_messages=lambda sid: self.list_session_messages(sid),
                list_session_events=lambda sid: self.list_session_events(sid),
            )

            is_parallel = chunk_kind == "parallel" and len(chunk_tcs) > 1
            executions = await self.tool_executor.execute_tool_calls(
                session_id, chunk_tcs, hooks=hooks, parallel=is_parallel
            )

            for execution in executions:
                result = execution["result"]
                if result.get("awaitUserResponse") is True:
                    waiting = True
                result_meta = result.get("metadata") if isinstance(result, dict) else None
                if isinstance(result_meta, dict) and result_meta.get("exitPlanMode"):
                    self._update_entry(
                        session_id,
                        lambda e: {**e, "planMode": False, "updateTime": _now()},
                    )

                exec_tc_id = execution["toolCallId"]
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

        return waiting

    async def _create_completion_with_retry(
        self, session_id: str, client: Any, request: dict[str, Any]
    ) -> dict[str, Any]:
        """Retry retryable LLM failures as new requests; do not persist failed chunks."""
        last_error: Exception | None = None
        for attempt in range(DEFAULT_MAX_RETRIES + 1):
            if self.is_interrupted(session_id):
                raise asyncio.CancelledError()
            try:
                response = await self._create_completion(client, request)
            except Exception as err:
                last_error = err
                code = classify_llm_failure(err)
                if code is None or attempt >= DEFAULT_MAX_RETRIES:
                    raise
                delay_s = retry_delay_ms(attempt + 1) / 1000.0
                await asyncio.sleep(delay_s)
                continue
            if is_empty_llm_response(response) and attempt < DEFAULT_MAX_RETRIES:
                delay_s = retry_delay_ms(attempt + 1) / 1000.0
                await asyncio.sleep(delay_s)
                continue
            return response
        if last_error is not None:
            raise last_error
        raise RuntimeError("EMPTY_RESPONSE: model returned no content after retries")

    async def _create_completion(
        self, client: Any, request: dict[str, Any], *, emit_stream: bool = True
    ) -> dict[str, Any]:
        on_chunk = self.on_stream_chunk if emit_stream else None
        on_thinking = self.on_thinking_chunk if emit_stream else None
        result = await asyncio.to_thread(
            _call_stream_or_sync,
            client,
            request,
            on_chunk,
            self.on_llm_stream_progress,
            on_thinking,
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

    async def _compact_session(self, session_id: str, trigger: str = "pressure") -> None:
        """Execute session compaction through the pluggable CompactionEngine."""
        res = await self.compaction_engine.compact_if_needed(session_id, trigger=trigger)
        if not res:
            res = await self.compaction_engine.compact_now(session_id, trigger=trigger)
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

    async def compact_session(self, session_id: str, trigger: str = "manual") -> None:
        """Public method to compact long session context history."""
        await self._compact_session(session_id, trigger=trigger)

    # ---- queries, delete, fork & undo ----

    def delete_session(self, session_id: str) -> bool:
        """Remove session messages and entry from index."""
        index = self._load_index()
        initial_len = len(index["entries"])
        index["entries"] = [e for e in index["entries"] if e.get("id") != session_id]
        if len(index["entries"]) == initial_len:
            return False

        self._save_index(index)
        msg_file = self._messages_path(session_id)
        if msg_file.exists():
            try:
                msg_file.unlink()
            except Exception:
                pass

        clear_session_state(session_id)
        ctrl = self.session_controllers.pop(session_id, None)
        if ctrl and not ctrl.is_set():
            ctrl.set()
        images_dir = self._storage()["project_dir"] / "images" / session_id
        if images_dir.exists():
            try:
                shutil.rmtree(images_dir)
            except Exception:
                pass
        return True

    def rename_session(self, session_id: str, new_title: str) -> bool:
        """Rename an existing session title/summary in index and memory."""
        cleaned_title = (new_title or "").strip()
        if not cleaned_title:
            return False
        entry = self._get_entry(session_id)
        if not entry:
            return False
        self._update_entry(
            session_id,
            lambda e: {**e, "summary": cleaned_title, "updateTime": _now()},
        )
        return True

    def fork_session(
        self,
        source_session_id: str,
        at_message_id_or_seq: str | int | None = None,
    ) -> str | None:
        """Fork an existing session into a new independent session branch with cloned message history, event logs, and file checkpoint."""
        src_entry = self._get_entry(source_session_id)
        if not src_entry:
            return None

        forked_id = f"ses_{uuid.uuid4().hex[:12]}"
        now = _now()

        raw_lines = self.session_store.read_raw_lines(source_session_id)

        sliced_lines: list[str] = []
        checkpoint_hash: str | None = None

        if at_message_id_or_seq is None:
            sliced_lines = list(raw_lines)
        elif isinstance(at_message_id_or_seq, int):
            # Check if matching by seq number or message index
            matched_by_seq = False
            for line in raw_lines:
                try:
                    data = json.loads(line)
                    if "seq" in data:
                        if int(data["seq"]) <= at_message_id_or_seq:
                            sliced_lines.append(line)
                            matched_by_seq = True
                    elif not matched_by_seq and len(sliced_lines) <= at_message_id_or_seq:
                        sliced_lines.append(line)
                except Exception:
                    continue
            if not matched_by_seq and not sliced_lines and raw_lines:
                # Fallback to index-based slice
                sliced_lines = raw_lines[: at_message_id_or_seq + 1]
        elif isinstance(at_message_id_or_seq, str):
            for line in raw_lines:
                sliced_lines.append(line)
                try:
                    data = json.loads(line)
                    if data.get("id") == at_message_id_or_seq:
                        break
                except Exception:
                    continue

        # Find latest checkpoint hash in the sliced messages/events
        for line in reversed(sliced_lines):
            try:
                data = json.loads(line)
                meta = data.get("meta") or data.get("data", {}).get("meta") or {}
                if isinstance(meta, dict) and meta.get("checkpointHash"):
                    checkpoint_hash = meta["checkpointHash"]
                    break
            except Exception:
                continue

        # Write cloned lines to the new session's file, rewriting sessionId
        forked_lines: list[str] = []
        for line in sliced_lines:
            try:
                data = json.loads(line)
                if "sessionId" in data:
                    data["sessionId"] = forked_id
                elif "session_id" in data:
                    data["session_id"] = forked_id
                forked_lines.append(json.dumps(data, ensure_ascii=False))
            except Exception:
                forked_lines.append(line)

        self.session_store.write_raw_lines(forked_id, forked_lines)

        # Fork file history branch
        self.file_history.ensure_session(forked_id)
        self.file_history.fork_session(
            source_session_id, forked_id, checkpoint_hash=checkpoint_hash
        )

        # Update sessions index
        index = self._load_index()
        forked_entry = {
            "id": forked_id,
            "summary": f"[Fork] {src_entry.get('summary', '')}",
            "assistantReply": src_entry.get("assistantReply"),
            "assistantThinking": src_entry.get("assistantThinking"),
            "assistantRefusal": None,
            "toolCalls": src_entry.get("toolCalls"),
            "status": "completed",
            "failReason": None,
            "askPermissions": None,
            "usage": None,
            "usagePerModel": None,
            "activeTokens": src_entry.get("activeTokens", 0),
            "createTime": now,
            "updateTime": now,
            "planMode": src_entry.get("planMode", False),
            "forkOf": source_session_id,
            "parentSessionId": source_session_id,
            "forkPoint": at_message_id_or_seq,
        }
        index["entries"].insert(0, forked_entry)
        index["entries"] = index["entries"][:MAX_SESSION_ENTRIES]
        self._save_index(index)

        return forked_id

    def list_undo_targets(self, session_id: str) -> list[dict[str, Any]]:
        """Return all undoable user turns with checkpoint hashes and prompt previews in chronological order."""
        messages = self.list_session_messages(session_id)
        user_messages = [m for m in messages if m.role == "user" and not m.compacted and m.visible]
        if not user_messages:
            return []

        targets: list[dict[str, Any]] = []
        for idx, m in enumerate(user_messages, 1):
            ckpt_hash = (m.meta or {}).get("checkpointHash")
            if not ckpt_hash:
                ckpt_hash = self.file_history.get_current_checkpoint_hash(session_id)

            can_restore_code = bool(
                ckpt_hash and self.file_history.can_restore(session_id, ckpt_hash)
            )
            raw_p = (m.meta or {}).get("rawPrompt")
            if raw_p and isinstance(raw_p, str):
                prompt_preview = raw_p.strip().splitlines()[0] if raw_p else "(empty prompt)"
            else:
                prompt_text = m.content or ""
                if "\n\n---\n\n" in prompt_text:
                    prompt_text = prompt_text.split("\n\n---\n\n", 1)[1]
                prompt_preview = (
                    prompt_text.strip().splitlines()[0] if prompt_text else "(empty prompt)"
                )
            targets.append(
                {
                    "index": idx,
                    "turn_index": idx,
                    "message_id": m.id,
                    "prompt": prompt_preview,
                    "full_prompt": m.content,
                    "create_time": m.create_time,
                    "checkpoint_hash": ckpt_hash,
                    "can_restore_code": can_restore_code,
                }
            )
        return targets

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
        messages = self.list_session_messages(session_id)
        user_messages = [m for m in messages if m.role == "user" and not m.compacted and m.visible]
        if not user_messages:
            return False

        if target_message_id:
            target_user = next((m for m in user_messages if m.id == target_message_id), None)
            if not target_user:
                return False
        else:
            target_user = user_messages[-1]

        checkpoint_hash = (target_user.meta or {}).get("checkpointHash")
        if not checkpoint_hash:
            checkpoint_hash = self.file_history.get_current_checkpoint_hash(session_id)

        # Restore code if requested
        if mode in ("restore_both", "restore_code_only"):
            if not checkpoint_hash or not self.file_history.can_restore(
                session_id, checkpoint_hash
            ):
                if mode == "restore_code_only":
                    return False
            else:
                self.file_history.restore(session_id, checkpoint_hash)

        # Restore conversation if requested
        if mode in ("restore_both", "restore_conversation_only"):
            cutoff_idx = next((i for i, m in enumerate(messages) if m.id == target_user.id), -1)
            if cutoff_idx >= 0:
                retained_messages = messages[:cutoff_idx]
                self._save_messages(session_id, retained_messages)
                clear_session_state(session_id)
                rebuild_session_state_from_history(
                    session_id, [self._serialize_message(m) for m in retained_messages]
                )

        self._update_entry(
            session_id,
            lambda e: {
                **e,
                "status": "completed",
                "updateTime": _now(),
            },
        )
        return True

    def list_sessions(self) -> list[SessionEntry]:
        return [_entry_from_dict(e) for e in self._load_index()["entries"]]

    def get_session(self, session_id: str) -> SessionEntry | None:
        entry = self._get_entry(session_id)
        return _entry_from_dict(entry) if entry else None

    async def init_mcp_servers(self) -> None:
        await self.mcp_manager.initialize(self.get_resolved_settings().get("mcpServers"))
        self._refresh_mcp_tool_definitions()

    async def sync_mcp_servers(self) -> None:
        """Sync MCP servers with current resolved settings at runtime."""
        settings = self.get_resolved_settings()
        await self.mcp_manager.sync_servers(settings.get("mcpServers"))
        self._refresh_mcp_tool_definitions()

    def _refresh_mcp_tool_definitions(self) -> None:
        self.mcp_tool_definitions = self.mcp_manager.get_mcp_tool_definitions()

    def dispose(self) -> None:
        """Best-effort sync dispose. Prefer ``close_session_manager`` from an async context."""
        for event in self.session_controllers.values():
            try:
                event.set()
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
    return SessionEntry(
        id=d.get("id", ""),
        summary=d.get("summary", ""),
        assistant_reply=d.get("assistantReply"),
        assistant_thinking=d.get("assistantThinking"),
        assistant_refusal=d.get("assistantRefusal"),
        tool_calls=d.get("toolCalls"),
        status=d.get("status", "pending"),
        fail_reason=d.get("failReason"),
        ask_permissions=d.get("askPermissions"),
        processes=d.get("processes"),
        usage=d.get("usage"),
        usage_per_model=d.get("usagePerModel"),
        active_tokens=d.get("activeTokens", 0),
        create_time=d.get("createTime", ""),
        update_time=d.get("updateTime", ""),
        plan_mode=bool(d.get("planMode")),
        fork_of=d.get("forkOf") or d.get("parentSessionId"),
        parent_session_id=d.get("parentSessionId") or d.get("forkOf"),
        fork_point=d.get("forkPoint"),
    )


def _normalize_tool_calls(raw: Any) -> list[dict[str, Any]] | None:
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


def _call_stream_or_sync(
    client: Any,
    request: dict[str, Any],
    on_chunk: Callable[[str], None] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    on_thinking_chunk: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    stream_req = {
        **request,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    try:
        try:
            resp = client.chat.completions.create(**stream_req)
        except Exception as err:
            err_msg = str(err).lower()
            if "reasoning_effort" in err_msg or "stream_options" in err_msg:
                retry_req = dict(stream_req)
                if "stream_options" in err_msg:
                    retry_req.pop("stream_options", None)
                if "reasoning_effort" in err_msg:
                    if "none" in err_msg:
                        retry_req["reasoning_effort"] = "none"
                    else:
                        retry_req.pop("reasoning_effort", None)
                        if isinstance(retry_req.get("extra_body"), dict):
                            retry_req["extra_body"].pop("reasoning_effort", None)
                            if not retry_req["extra_body"]:
                                retry_req.pop("extra_body", None)
                resp = client.chat.completions.create(**retry_req)
            else:
                raise

        if isinstance(resp, dict):
            return resp

        if hasattr(resp, "choices"):
            return _format_completion_response(resp)

        if hasattr(resp, "__iter__"):
            content_parts: list[str] = []
            thinking_parts: list[str] = []
            tool_calls_dict: dict[int, dict[str, Any]] = {}
            refusal_parts: list[str] = []
            usage_dict: dict[str, int] = {}
            estimated_tokens = 0

            try:
                for chunk in resp:
                    choices = getattr(chunk, "choices", None) or []
                    if choices:
                        c = choices[0]
                        delta = getattr(c, "delta", None)
                        if delta:
                            delta_content = getattr(delta, "content", None)
                            if delta_content:
                                content_parts.append(delta_content)
                                estimated_tokens += max(1, len(delta_content) // 4)
                                if on_chunk:
                                    on_chunk(delta_content)
                                if on_progress:
                                    on_progress(
                                        {"estimatedTokens": estimated_tokens, "type": "update"}
                                    )

                            delta_thinking = getattr(delta, "reasoning_content", None) or getattr(
                                delta, "thinking", None
                            )
                            if delta_thinking:
                                thinking_parts.append(delta_thinking)
                                estimated_tokens += max(1, len(delta_thinking) // 4)
                                if on_thinking_chunk:
                                    on_thinking_chunk(delta_thinking)
                                if on_progress:
                                    on_progress(
                                        {
                                            "estimatedTokens": estimated_tokens,
                                            "type": "update",
                                            "isThinking": True,
                                        }
                                    )

                            delta_refusal = getattr(delta, "refusal", None)
                            if delta_refusal:
                                refusal_parts.append(delta_refusal)

                            delta_tc = getattr(delta, "tool_calls", None)
                            if delta_tc:
                                for tc_delta in delta_tc:
                                    idx = getattr(tc_delta, "index", 0)
                                    if idx not in tool_calls_dict:
                                        tool_calls_dict[idx] = {
                                            "id": getattr(tc_delta, "id", "") or uuid.uuid4().hex,
                                            "type": "function",
                                            "function": {"name": "", "arguments": ""},
                                        }
                                    entry = tool_calls_dict[idx]
                                    if getattr(tc_delta, "id", None):
                                        entry["id"] = tc_delta.id
                                    func = getattr(tc_delta, "function", None)
                                    if func:
                                        if getattr(func, "name", None):
                                            entry["function"]["name"] += func.name
                                        if getattr(func, "arguments", None):
                                            entry["function"]["arguments"] += func.arguments

                    chunk_usage = getattr(chunk, "usage", None)
                    if chunk_usage:
                        usage_dict = extract_usage_dict(chunk_usage)
            except Exception as stream_err:
                # If stream failed mid-generation or failed with API error, re-raise to avoid duplicate sync request
                raise stream_err

            if on_progress:
                on_progress({"estimatedTokens": estimated_tokens, "type": "end"})

            # Fallback to estimated token counts if upstream API did not return usage in stream
            if not usage_dict and estimated_tokens > 0:
                usage_dict = {
                    "prompt_tokens": 0,
                    "completion_tokens": estimated_tokens,
                    "total_tokens": estimated_tokens,
                    "cached_tokens": 0,
                    "prompt_cache_hit_tokens": 0,
                    "prompt_cache_miss_tokens": 0,
                }

            # Repair and assemble tool calls
            assembled_tool_calls: list[dict[str, Any]] = []
            for i in sorted(tool_calls_dict.keys()):
                tc = tool_calls_dict[i]
                raw_args = tc.get("function", {}).get("arguments", "")
                if raw_args:
                    tc["function"]["arguments"] = repair_json_string(raw_args)
                assembled_tool_calls.append(tc)

            tool_calls = assembled_tool_calls or None
            res: dict[str, Any] = {
                "choices": [
                    {
                        "message": {
                            "content": "".join(content_parts),
                            "tool_calls": tool_calls,
                            "reasoning_content": "".join(thinking_parts) or None,
                            "refusal": "".join(refusal_parts) or None,
                        }
                    }
                ]
            }
            if usage_dict:
                res["usage"] = usage_dict
            return res
    except (TypeError, AttributeError):
        # Client does not support streaming create() or response object format
        return _call_sync(client, request)
    return _call_sync(client, request)


def _format_completion_response(resp: Any) -> dict[str, Any]:
    if isinstance(resp, dict):
        return resp
    message = getattr(resp.choices[0], "message", None)
    result: dict[str, Any] = {
        "choices": [
            {
                "message": {
                    "content": getattr(message, "content", None) or "",
                    "tool_calls": _pydantic_tool_calls(message),
                    "reasoning_content": getattr(message, "reasoning_content", None),
                    "refusal": getattr(message, "refusal", None),
                }
            }
        ]
    }
    usage = getattr(resp, "usage", None)
    if usage is not None:
        result["usage"] = extract_usage_dict(usage)
    return result


def _call_sync(client: Any, request: dict[str, Any]) -> dict[str, Any]:
    resp = client.chat.completions.create(**request)
    return _format_completion_response(resp)


def _pydantic_tool_calls(message: Any) -> list[dict[str, Any]] | None:
    raw = getattr(message, "tool_calls", None)
    if not raw:
        return None
    out: list[dict[str, Any]] = []
    for tc in raw:
        func = getattr(tc, "function", None)
        out.append(
            {
                "id": getattr(tc, "id", "") or uuid.uuid4().hex,
                "type": "function",
                "function": {
                    "name": getattr(func, "name", "") or "",
                    "arguments": getattr(func, "arguments", "") or "",
                },
            }
        )
    return out or None


def _accumulate_usage(
    current: dict[str, Any] | None, usage: dict[str, Any] | None
) -> dict[str, Any] | None:
    return accumulate_usage_dict(current, usage)


def _accumulate_usage_per_model(
    current: dict[str, Any] | None, model: str, usage: dict[str, Any] | None
) -> dict[str, Any] | None:
    if usage is None or not model:
        return current
    res = dict(current or {})
    res[model] = _accumulate_usage(res.get(model), usage)
    return res


def _total_tokens(usage: dict[str, Any] | None) -> int:
    return usage.get("total_tokens", 0) if usage else 0


def _copy_with(m: SessionMessage, **changes: Any) -> SessionMessage:
    return SessionMessage(
        id=m.id,
        session_id=m.session_id,
        role=m.role,
        content=m.content,
        tool_calls=m.tool_calls,
        tool_call_id=m.tool_call_id,
        thinking=m.thinking,
        compacted=changes.get("compacted", m.compacted),
        visible=m.visible,
        create_time=m.create_time,
        update_time=changes.get("update_time", m.update_time),
        meta=m.meta,
    )


def _resolve_target_file_path(session_id: str, project_root: str, tc: Any) -> str | None:
    if isinstance(tc, dict):
        args_raw = tc.get("function", {}).get("arguments", "{}")
    else:
        func = getattr(tc, "function", None)
        args_raw = getattr(func, "arguments", "{}") if func else "{}"
    try:
        args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
    except Exception:
        args = {}
    if not isinstance(args, dict):
        return None
    fp = args.get("file_path")
    if isinstance(fp, str) and fp.strip():
        p = pathlib.Path(fp)
        return (
            str(p.resolve()) if p.is_absolute() else str((pathlib.Path(project_root) / p).resolve())
        )
    snippet_id = args.get("snippet_id")
    if isinstance(snippet_id, str) and snippet_id.strip():
        return resolve_snippet_file_path(session_id, snippet_id)
    return None


def _is_invisible_execution(content: str) -> bool:
    try:
        data = json.loads(content)
        return bool(data.get("metadata", {}).get("invisible") is True)
    except Exception:
        return False


def _build_tool_params_snippet(tool_function: Any) -> str:
    if not tool_function:
        return ""
    if isinstance(tool_function, dict):
        name = tool_function.get("name", "")
        args = tool_function.get("arguments", "{}")
    else:
        name = getattr(tool_function, "name", "")
        args = getattr(tool_function, "arguments", "{}")
    return f"`{name}`: {str(args)[:100]}"


def _build_tool_result_snippet(content: str) -> str:
    try:
        data = json.loads(content)
        if data.get("ok"):
            out = str(data.get("output") or "OK")
            return out[:100] + ("..." if len(out) > 100 else "")
        err = str(data.get("error") or "Error")
        return f"Error: {err[:100]}"
    except Exception:
        return content[:100]
