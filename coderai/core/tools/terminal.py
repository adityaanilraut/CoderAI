"""Terminal tools — persistent interactive PTY sessions."""

from __future__ import annotations

import json
import time
from typing import Any

from coderai.core.jobs import get_job_store
from coderai.core.terminal.manager import get_terminal_manager
from coderai.core.tools.types import ToolResult, as_str

DEFAULT_SEND_TIMEOUT_S = 30.0
DEFAULT_READ_TIMEOUT_S = 2.0


def handle_terminal_open_tool(args: dict[str, Any], context: Any) -> ToolResult:
    """Open a persistent interactive terminal session."""
    shell_type = as_str(args.get("type", "bash")).strip() or "bash"
    name = as_str(args.get("name", "")).strip() or None
    cwd = as_str(args.get("cwd", "")).strip() or None

    project_root = getattr(context, "project_root", ".") if context else "."
    sandbox_mode = getattr(context, "sandbox_mode", None)
    if isinstance(context, dict):
        sandbox_mode = context.get("sandbox_mode", sandbox_mode)
        project_root = context.get("project_root", project_root)

    if not cwd:
        cwd = project_root

    mgr = get_terminal_manager()
    try:
        term = mgr.open_session(
            command=shell_type,
            name=name,
            cwd=cwd,
            sandbox_mode=sandbox_mode,
            workspace_root=str(project_root),
        )
        # Give the shell a moment to emit initial prompt
        initial_output = term.read_available(timeout_s=0.2)
        out = {
            "sessionId": term.session_id,
            "name": term.name,
            "type": term.process_type,
            "pid": term.pid,
            "cwd": term.cwd,
            "output": initial_output,
        }
        if getattr(term, "_sandbox_meta", None):
            out["sandbox"] = term._sandbox_meta
        return ToolResult(
            ok=True,
            name="terminal_open",
            output=json.dumps(out, indent=2),
            metadata=out,
        )
    except Exception as exc:
        return ToolResult(
            ok=False,
            name="terminal_open",
            error=f"Failed to open terminal session: {exc}",
        )


def handle_terminal_send_tool(args: dict[str, Any], context: Any) -> ToolResult:
    """Send text to an open terminal session."""
    session_id = as_str(args.get("sessionId") or args.get("session_id", "")).strip()
    text = as_str(args.get("text", ""))
    submit = bool(args.get("submit", True))
    run_in_background = bool(args.get("run_in_background", False))
    timeout_ms = args.get("timeout_ms")
    timeout_s = (float(timeout_ms) / 1000.0) if timeout_ms else DEFAULT_SEND_TIMEOUT_S

    if not session_id:
        return ToolResult(
            ok=False,
            name="terminal_send",
            error="Parameter `sessionId` is required.",
        )

    mgr = get_terminal_manager()
    term = mgr.get_session(session_id)
    if not term:
        return ToolResult(
            ok=False,
            name="terminal_send",
            error=f"Terminal session `{session_id}` not found or already closed.",
        )

    if not term.is_alive:
        return ToolResult(
            ok=False,
            name="terminal_send",
            error=f"Terminal session `{session_id}` has exited with code {term.exit_code}.",
        )

    try:
        term.send(text, submit=submit)

        if run_in_background:
            sess_id = str(getattr(context, "session_id", "default") or "default")
            job_id = f"job_pty_{int(time.time() * 1000)}"
            store = get_job_store()
            job = store.start(
                job_id=job_id,
                session_id=sess_id,
                kind="pty-send",
                label=f"terminal_send({session_id}): {text[:50]}",
                process_id=term.pid,
            )
            bg_out: dict[str, Any] = {
                "sessionId": session_id,
                "jobId": job.id,
                "status": "background",
                "message": f"Command sent to terminal `{session_id}` in background. Use job_output(job_id='{job.id}') or terminal_read(sessionId='{session_id}') to collect output.",
            }
            return ToolResult(
                ok=True,
                name="terminal_send",
                output=json.dumps(bg_out, indent=2),
                metadata=bg_out,
            )

        # Synchronous read up to timeout
        output = term.read_available(timeout_s=timeout_s)
        sync_out: dict[str, Any] = {
            "sessionId": session_id,
            "output": output,
            "isAlive": term.is_alive,
            "exitCode": term.exit_code,
        }
        return ToolResult(
            ok=True,
            name="terminal_send",
            output=json.dumps(sync_out, indent=2),
            metadata=sync_out,
        )
    except Exception as exc:
        return ToolResult(
            ok=False,
            name="terminal_send",
            error=f"Failed to send to terminal `{session_id}`: {exc}",
        )


def handle_terminal_read_tool(args: dict[str, Any], context: Any) -> ToolResult:
    """Read pending or accumulated output from a terminal session."""
    session_id = as_str(args.get("sessionId") or args.get("session_id", "")).strip()
    timeout_ms = args.get("timeout_ms")
    timeout_s = (float(timeout_ms) / 1000.0) if timeout_ms else DEFAULT_READ_TIMEOUT_S

    if not session_id:
        return ToolResult(
            ok=False,
            name="terminal_read",
            error="Parameter `sessionId` is required.",
        )

    mgr = get_terminal_manager()
    term = mgr.get_session(session_id)
    if not term:
        return ToolResult(
            ok=False,
            name="terminal_read",
            error=f"Terminal session `{session_id}` not found.",
        )

    output = term.read_available(timeout_s=timeout_s)
    out = {
        "sessionId": session_id,
        "output": output,
        "isAlive": term.is_alive,
        "exitCode": term.exit_code,
    }
    return ToolResult(
        ok=True,
        name="terminal_read",
        output=json.dumps(out, indent=2),
        metadata=out,
    )


def handle_terminal_signal_tool(args: dict[str, Any], context: Any) -> ToolResult:
    """Send a signal (e.g. SIGINT, SIGTERM, SIGKILL) to a terminal session."""
    session_id = as_str(args.get("sessionId") or args.get("session_id", "")).strip()
    sig = as_str(args.get("signal", "SIGINT")).strip()

    if not session_id:
        return ToolResult(
            ok=False,
            name="terminal_signal",
            error="Parameter `sessionId` is required.",
        )

    mgr = get_terminal_manager()
    term = mgr.get_session(session_id)
    if not term:
        return ToolResult(
            ok=False,
            name="terminal_signal",
            error=f"Terminal session `{session_id}` not found.",
        )

    try:
        term.send_signal(sig)
        time.sleep(0.05)
        out = {
            "sessionId": session_id,
            "signal": sig,
            "isAlive": term.is_alive,
            "exitCode": term.exit_code,
        }
        return ToolResult(
            ok=True,
            name="terminal_signal",
            output=json.dumps(out, indent=2),
            metadata=out,
        )
    except Exception as exc:
        return ToolResult(
            ok=False,
            name="terminal_signal",
            error=f"Failed to send signal `{sig}` to terminal `{session_id}`: {exc}",
        )


def handle_terminal_close_tool(args: dict[str, Any], context: Any) -> ToolResult:
    """Close and terminate an active terminal session."""
    session_id = as_str(args.get("sessionId") or args.get("session_id", "")).strip()
    if not session_id:
        return ToolResult(
            ok=False,
            name="terminal_close",
            error="Parameter `sessionId` is required.",
        )

    mgr = get_terminal_manager()
    closed = mgr.close_session(session_id)
    if not closed:
        return ToolResult(
            ok=False,
            name="terminal_close",
            error=f"Terminal session `{session_id}` not found or already closed.",
        )

    return ToolResult(
        ok=True,
        name="terminal_close",
        output=f"Terminal session `{session_id}` closed successfully.",
    )


def handle_terminal_list_tool(args: dict[str, Any], context: Any) -> ToolResult:
    """List all open persistent terminal sessions."""
    mgr = get_terminal_manager()
    sessions = mgr.list_sessions()
    out = [s.to_dict() for s in sessions]
    return ToolResult(
        ok=True,
        name="terminal_list",
        output=json.dumps(out, indent=2),
        metadata={"sessions": out},
    )
