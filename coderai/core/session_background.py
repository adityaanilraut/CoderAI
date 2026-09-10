"""Background process and schedule tracking helpers for session manager."""

from __future__ import annotations

import time
from typing import Any

from coderai.core.common.file_utils import read_text_file_tail
from coderai.core.session_models import _now
from coderai.core.tools.types import BackgroundProcessCompletion

BACKGROUND_FAILURE_LOG_TAIL_CHARS = 4000


def build_background_failure_log_tail_slice(output_path: str | None) -> str | None:
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


def dispatch_due_schedules(manager: Any, session_id: str) -> bool:
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
            msg = manager._build_message(session_id, "user", reminder_text)
            manager._append_message(msg)
            if manager.on_user_message:
                manager.on_user_message(msg)
        return True
    except Exception:
        return False


def add_background_process_completion_message(
    manager: Any, session_id: str, completion: BackgroundProcessCompletion
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
        else build_background_failure_log_tail_slice(completion.output_path)
    )
    content = f"{base_content}\n{log_tail}" if log_tail else base_content

    msg = manager._build_message(
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
    manager._append_message(msg)
    try:
        manager.notify(
            base_content,
            log_tail or "",
            category="task",
            type="background_task_completed" if completion.ok else "background_task_failed",
            source_kind="background_task",
            source_id=completion.task_id,
            severity="success" if completion.ok else "error",
            targets=["wire", "shell"],
            dedupe_key=f"bg-{completion.task_id}-{status}",
            payload={"sessionId": session_id, "ok": completion.ok},
        )
    except Exception:
        pass


def track_process_start(manager: Any, session_id: str, pid: int | str, command: str) -> None:
    pid_key = str(pid)
    manager._update_entry(
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


def track_process_exit(manager: Any, session_id: str, pid: int | str) -> None:
    pid_key = str(pid)

    def mutate(e: dict[str, Any]) -> dict[str, Any]:
        procs = dict(e.get("processes") or {})
        procs.pop(pid_key, None)
        return {**e, "processes": procs}

    manager._update_entry(session_id, mutate)


def kill_live_processes(manager: Any, session_id: str | None = None) -> None:
    """Kill all tracked live processes for a session."""
    from coderai.core.tools.bash import kill_process_tree

    if not session_id:
        return
    entry = manager._get_entry(session_id)
    if entry and entry.get("processes"):
        for pid_str in list(entry["processes"].keys()):
            try:
                pid = int(pid_str)
                kill_process_tree(pid)
            except Exception:
                pass
        manager._update_entry(
            session_id,
            lambda e: {**e, "processes": {}, "updateTime": _now()},
        )


def maybe_notify_task_completion(manager: Any, session_id: str, started_at_ms: int) -> None:
    """Trigger configured notification command when session finishes."""
    from coderai.core.common.notify import launch_notify_script

    settings = manager.get_resolved_settings()
    notify_command = settings.get("notify")
    if not notify_command:
        return

    entry = manager._get_entry(session_id)
    status = entry.get("status", "completed") if entry else "completed"
    fail_reason = entry.get("failReason") if entry else None
    duration_ms = max(0, int(time.time() * 1000) - started_at_ms)

    messages = manager.list_session_messages(session_id)
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
        working_directory=manager.project_root,
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
