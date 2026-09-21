"""bash tool — subprocess with timeout, background execution, and persistent cwd."""

from __future__ import annotations

import os
import pathlib
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from typing import Any

from coderai.utils.subprocess_env import DEFAULT_BASH_TIMEOUT_MS, clamp_bash_timeout_ms
from coderai.utils.subprocess_env import kill_process_tree
from coderai.background import get_job_store
from coderai.sandbox import (
    SandboxUnavailableError,
    check_sandbox_path_access,
    resolve_exec_cwd,
    wrap_sandbox_command,
)
from coderai.tools.legacy.path_lock import extract_redirect_paths
from coderai.tools.legacy.sanitizer import sanitize_text
from coderai.spill import apply_spill_policy
from coderai.utils.shell_quoting import (
    build_disable_extglob_command,
    build_shell_env,
    build_shell_init_command,
    resolve_shell_path,
    rewrite_windows_null_redirect,
    to_native_cwd,
)
from coderai.terminal.manager import get_terminal_manager
from coderai.tools.legacy.types import (
    BackgroundProcessCompletion,
    ProcessTimeoutControl,
    ProcessTimeoutInfo,
    ToolResult,
    as_str,
)

MAX_OUTPUT_CHARS = 30000
MAX_CAPTURE_CHARS = 10 * 1024 * 1024
BACKGROUND_OUTPUT_DIR = pathlib.Path(tempfile.gettempdir()) / "coderai-background"
TRAILING_BACKGROUND_OPERATOR_PATTERN = re.compile(r"(^|[^\\&])\s*&\s*$")

session_working_dirs: dict[str, str] = {}


def clear_session_working_dir(session_id: str) -> None:
    if session_id:
        session_working_dirs.pop(session_id, None)


def _is_true(value: Any) -> bool:
    return value is True or str(value).lower() in ("true", "1", "yes")


def _strip_trailing_background_operator(command: str) -> str:
    return TRAILING_BACKGROUND_OPERATOR_PATTERN.sub(r"\1", command).rstrip()


def _get_session_cwd(session_id: str, fallback: str) -> str:
    return session_working_dirs.get(session_id, fallback)


def _update_session_cwd(session_id: str, fallback: str, cwd: str | None) -> None:
    next_cwd = cwd or fallback
    if next_cwd and os.path.isdir(next_cwd):
        session_working_dirs[session_id] = next_cwd


def _context_value(context: Any, name: str, default: Any = None) -> Any:
    if isinstance(context, dict):
        return context.get(name, default)
    return getattr(context, name, default)


def _isolated_root(context: Any) -> str | None:
    iso = _context_value(context, "isolated_cwd", None)
    if isinstance(iso, (str, pathlib.Path)) and str(iso).strip():
        return str(iso)
    return None


_SANDBOX_RANK = {"read-only": 0, "workspace-write": 1, "danger-full-access": 2}


def _effective_sandbox_mode(context: Any, args: dict[str, Any]) -> tuple[Any, ToolResult | None]:
    """Wire the `sandbox_permissions` escalation arg (fail-closed).

    Returns (mode_to_enforce, error_result). An unknown mode, or any escalation
    to a more permissive mode than the session base without a justification, is
    denied instead of silently running with session permissions.
    """
    from coderai.sandbox import parse_sandbox_mode

    base = _context_value(context, "sandbox_mode", None)
    requested = args.get("sandbox_permissions") if isinstance(args, dict) else None
    if requested is None or (isinstance(requested, str) and not requested.strip()):
        return base, None
    parsed = parse_sandbox_mode(requested) if isinstance(requested, str) else None
    if parsed is None:
        return base, ToolResult(
            ok=False, name="bash", error=f"invalid sandbox_permissions: {requested!r}."
        )
    base_parsed = parse_sandbox_mode(base) if isinstance(base, str) else None
    base_rank = _SANDBOX_RANK.get(base_parsed or "workspace-write", 1)
    if _SANDBOX_RANK[parsed] > base_rank:
        justification = args.get("justification") if isinstance(args, dict) else None
        if not isinstance(justification, str) or not justification.strip():
            return base, ToolResult(
                ok=False,
                name="bash",
                error=(
                    f"sandbox_permissions escalation to '{parsed}' requires a "
                    "non-empty justification."
                ),
            )
    return parsed, None


def _reject_escaping_redirects(
    command: str,
    start_cwd: str,
    project_root: str,
    isolated: str | None,
    mode: Any,
    tool_name: str = "bash",
) -> ToolResult | None:
    """Deny shell `>`/`>>` targets that escape the execution root or sandbox.

    Redirects otherwise bypass per-path write locks and file sandbox checks.
    """
    for target in extract_redirect_paths(command or ""):
        candidate = target if os.path.isabs(target) else os.path.join(start_cwd, target)
        try:
            resolved = resolve_exec_cwd(candidate, project_root, isolated)
        except (ValueError, OSError):
            return ToolResult(
                ok=False,
                name=tool_name,
                error=(f"Shell redirect target '{target}' escapes the execution root; refusing."),
            )
        allowed, err = check_sandbox_path_access(
            resolved,
            op="write",
            mode=mode if isinstance(mode, str) else None,
            workspace_root=project_root,
        )
        if not allowed:
            return ToolResult(
                ok=False,
                name=tool_name,
                error=err or f"Shell redirect target '{target}' blocked by sandbox policy.",
            )
    return None


def _build_marker() -> str:
    token = secrets.token_hex(6)
    return f"__CODERAI_PWD__{token}__"


def _build_shell_command(command: str) -> tuple[str, list[str], str]:
    shell_path = resolve_shell_path() or ("/bin/sh" if sys.platform != "win32" else "cmd.exe")
    marker = _build_marker()
    init_command = build_shell_init_command(shell_path)
    disable_extglob_command = build_disable_extglob_command(shell_path)
    normalized_command = rewrite_windows_null_redirect(command)
    wrapped_lines: list[str] = ["{"]
    if init_command:
        wrapped_lines.append(init_command)
    if disable_extglob_command:
        wrapped_lines.append(disable_extglob_command)
    wrapped_lines.append("export PAGER=cat NO_COLOR=1 2>/dev/null || true")
    wrapped_lines.append(normalized_command)
    wrapped_lines.extend(
        [
            "__CODERAI_STATUS__=$?",
            f'printf "%s%s\\n" "{marker}" "$PWD"',
            "exit $__CODERAI_STATUS__",
            "} < /dev/null",
        ]
    )
    wrapped_command = "\n".join(wrapped_lines)
    return shell_path, ["-c", wrapped_command], marker


def _strip_marker(output: str, marker: str) -> tuple[str, str | None]:
    if not output:
        return "", None
    lines = output.splitlines()
    marker_index = -1
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith(marker):
            marker_index = i
            break
    if marker_index == -1:
        return output, None
    marker_line = lines[marker_index]
    cwd_raw = marker_line[len(marker) :].strip()
    cwd = to_native_cwd(cwd_raw) if cwd_raw else None
    lines.pop(marker_index)
    return "\n".join(lines), cwd


def _join_output(stdout: str, stderr: str) -> str:
    trimmed_stdout = stdout or ""
    trimmed_stderr = stderr or ""
    if trimmed_stdout and trimmed_stderr:
        return f"{trimmed_stdout}\n{trimmed_stderr}"
    return trimmed_stdout or trimmed_stderr


def _sandbox_wrap(
    shell_path: str,
    shell_args: list[str],
    context: Any,
    cwd: str,
    mode_override: Any = None,
) -> tuple[list[str], dict[str, Any]]:
    if mode_override is not None:
        mode = mode_override
    else:
        mode = getattr(context, "sandbox_mode", None)
    project_root = getattr(context, "project_root", None) or cwd
    if isinstance(context, dict):
        if mode_override is None:
            mode = context.get("sandbox_mode", mode)
        project_root = context.get("project_root", project_root)
    return wrap_sandbox_command(
        [shell_path, *shell_args],
        mode=mode,
        workspace_root=str(project_root or cwd),
        cwd=cwd,
    )


def _truncate_output(output: str) -> tuple[str, bool]:
    if len(output) <= MAX_OUTPUT_CHARS:
        return output, False
    return output[:MAX_OUTPUT_CHARS], True


def _build_error_message(
    exit_code: int | None,
    signal_name: str | None,
    error: str | None = None,
    timed_out: bool = False,
) -> str:
    if error:
        return error
    if timed_out:
        return "Command timed out."
    if signal_name:
        return f"Command terminated by signal {signal_name}."
    if exit_code is not None:
        return f"Command failed with exit code {exit_code}."
    return "Command failed."


def _build_stop_command(pid: int) -> str:
    if sys.platform == "win32":
        return f'cmd.exe /c "taskkill /PID {pid} /T /F"'
    return f"kill -- -{pid}"


def _append_chunk(existing: str, chunk: str) -> str:
    if len(existing) >= MAX_CAPTURE_CHARS:
        return existing
    remaining = MAX_CAPTURE_CHARS - len(existing)
    return existing + chunk[:remaining]


ANSI_ESCAPE_PATTERN = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _strip_ansi_escapes(text: str) -> str:
    return ANSI_ESCAPE_PATTERN.sub("", text)


def _execute_persistent_bash(
    command: str,
    session_id: str,
    start_cwd: str,
    context: Any,
    args: dict[str, Any],
    mode_override: Any = None,
) -> ToolResult:
    mgr = get_terminal_manager()
    session_name = f"persistent_bash_{session_id}"
    if mode_override is not None:
        sandbox_mode = mode_override
    else:
        sandbox_mode = getattr(context, "sandbox_mode", None)
    project_root = getattr(context, "project_root", None) or start_cwd
    if isinstance(context, dict):
        if mode_override is None:
            sandbox_mode = context.get("sandbox_mode", sandbox_mode)
        project_root = context.get("project_root", project_root)

    term = mgr.get_session(session_name)
    if term is None or not term.is_alive:
        try:
            term = mgr.open_session(
                command="bash",
                name=session_name,
                cwd=start_cwd,
                sandbox_mode=sandbox_mode,
                workspace_root=str(project_root),
            )
            term.send("stty -echo 2>/dev/null || true; export PS1=''", submit=True)
            time.sleep(0.05)
            term.read_available(timeout_s=0.1)
        except Exception as e:
            return ToolResult(
                ok=False,
                name="bash",
                error=f"Failed to initialize persistent bash session: {e}",
            )

    # Flush unread prior output
    term.read_unread()

    token = secrets.token_hex(6)
    start_marker = f"__CODERAI_START_{token}__"
    end_marker = f"__CODERAI_END_{token}__"
    wrapped_cmd = f'printf "\\n%s\\n" "{start_marker}"; {command}; printf "\\n%s:%%d:%%s\\n" "{end_marker}" "$?" "$PWD"'

    timeout_ms = args.get("timeout_ms")
    timeout_s = (float(timeout_ms) / 1000.0) if timeout_ms else (DEFAULT_BASH_TIMEOUT_MS / 1000.0)

    try:
        term.send(wrapped_cmd, submit=True)
    except Exception as e:
        return ToolResult(
            ok=False,
            name="bash",
            error=f"Failed to write to persistent bash session: {e}",
        )

    accumulated = ""
    deadline = time.time() + timeout_s
    timed_out = False
    exit_code: int | None = None
    next_cwd: str | None = None

    while time.time() < deadline:
        chunk = term.read_unread()
        if chunk:
            accumulated += chunk
            if end_marker in accumulated:
                break
        else:
            time.sleep(0.02)
        if not term.is_alive:
            break

    if end_marker not in accumulated:
        if time.time() >= deadline:
            timed_out = True
            try:
                term.send_signal("SIGINT")
            except Exception:
                pass

    clean_accum = _strip_ansi_escapes(accumulated)
    body = clean_accum

    if start_marker in clean_accum and end_marker in clean_accum:
        after_start = clean_accum.split(start_marker)[-1]
        before_end, after_end = after_start.split(end_marker, 1)
        body = before_end.strip()
        meta_line = after_end.strip()
        m = re.match(r"^:(\d+):(.*)$", meta_line)
        if m:
            try:
                exit_code = int(m.group(1))
                next_cwd = m.group(2).splitlines()[0].strip() or None
            except ValueError:
                pass
    elif end_marker in clean_accum:
        body = clean_accum.split(end_marker, 1)[0].strip()

    if next_cwd and os.path.isdir(next_cwd):
        try:
            resolve_exec_cwd(next_cwd, str(project_root), _isolated_root(context))
        except (ValueError, OSError):
            next_cwd = None
        else:
            _update_session_cwd(session_id, start_cwd, next_cwd)

    # Sanitize BEFORE disk spill so spilled files never store raw secrets.
    cleaned_body = sanitize_text(body.strip())[0]
    spilled, spill_ref = apply_spill_policy(
        cleaned_body,
        session_id=str(session_id),
        tool_name="bash",
        max_inline_bytes=MAX_OUTPUT_CHARS,
        suggested_name="bash_persistent.txt",
    )
    if spill_ref is not None:
        truncated_text, is_truncated = spilled, True
    else:
        truncated_text, is_truncated = _truncate_output(cleaned_body)

    ok = (exit_code == 0 or exit_code is None) and not timed_out
    error_msg = None
    if not ok:
        if timed_out:
            error_msg = "Command timed out in persistent bash session."
        elif exit_code is not None and exit_code != 0:
            error_msg = f"Command failed with exit code {exit_code}."

    metadata: dict[str, Any] = {
        "exitCode": exit_code,
        "cwd": next_cwd or start_cwd,
        "truncated": is_truncated,
        "timedOut": timed_out,
        "persistent": True,
        "terminalSessionId": term.session_id,
    }
    if spill_ref is not None:
        metadata["spill"] = spill_ref.to_dict()

    return ToolResult(
        ok=ok,
        name="bash",
        output=truncated_text or "(no output)",
        error=error_msg,
        metadata=metadata,
    )


def handle(args: dict[str, Any], context: Any) -> ToolResult:
    return handle_bash_tool(args, context)


def handle_bash_tool(args: dict[str, Any], context: Any) -> ToolResult:
    raw_command = as_str(args.get("command"))
    run_in_background = _is_true(args.get("run_in_background"))
    persistent = _is_true(args.get("persistent"))
    command = _strip_trailing_background_operator(raw_command) if run_in_background else raw_command

    if not command.strip():
        return ToolResult(
            ok=False,
            name="bash",
            error="invalid command: expected a non-empty string",
        )
    # Description check: only required when run_in_background is True
    desc_val = args.get("description")
    if run_in_background and (desc_val is None or not str(desc_val).strip()):
        return ToolResult(
            ok=False,
            name="bash",
            error="invalid description: expected a non-empty string for background execution",
        )
    # timeoutMs must be positive number when provided
    if args.get("timeout_ms") is not None:
        try:
            tm = float(args["timeout_ms"])
            if not (tm == tm and tm > 0):
                raise ValueError
        except Exception:
            return ToolResult(
                ok=False,
                name="bash",
                error=f"invalid timeoutMs: expected a positive number, got {args['timeout_ms']!r}",
            )

    session_id = getattr(context, "session_id", None) or (
        context.get("session_id", "default") if isinstance(context, dict) else "default"
    )
    project_root = getattr(context, "project_root", None) or (
        context.get("project_root", os.getcwd()) if isinstance(context, dict) else os.getcwd()
    )

    eff_mode, mode_error = _effective_sandbox_mode(context, args)
    if mode_error is not None:
        return mode_error
    isolated = _isolated_root(context)
    try:
        start_cwd = resolve_exec_cwd(
            _get_session_cwd(session_id, project_root), str(project_root), isolated
        )
    except (ValueError, OSError) as exc:
        return ToolResult(ok=False, name="bash", error=f"CWD rejected: {exc}")
    redirect_error = _reject_escaping_redirects(
        command, start_cwd, str(project_root), isolated, eff_mode
    )
    if redirect_error is not None:
        return redirect_error

    if persistent and sys.platform != "win32":
        return _execute_persistent_bash(
            command, str(session_id), start_cwd, context, args, eff_mode
        )

    shell_path, shell_args, marker = _build_shell_command(command)

    if run_in_background:
        return _start_background_shell_command(
            shell_path, shell_args, start_cwd, command, marker, context, eff_mode
        )

    execution = _execute_shell_command(
        shell_path, shell_args, start_cwd, command, context, eff_mode
    )
    cleaned_stdout, cwd = _strip_marker(execution["stdout"], marker)
    combined = _join_output(cleaned_stdout, execution["stderr"])
    # Exit/signal/timeout markers appended to body
    body_with_marker = combined if combined.strip() else "(no output)"
    if execution["timed_out"]:
        body_with_marker = f"{body_with_marker}\n[timed out after {execution['timeout_ms']}ms]"
    elif execution["signal"]:
        body_with_marker = f"{body_with_marker}\n[killed by signal: {execution['signal']}]"
    else:
        body_with_marker = f"{body_with_marker}\n[exit code: {execution['exit_code']}]"
    # sandbox denial marker (when runner reports sandbox block)
    if execution.get("sandbox_denied"):
        mode = execution.get("sandbox_mode") or "unknown"
        body_with_marker += f"\n[sandbox: file access denied under {mode} mode]"

    # Sanitize BEFORE disk spill so spilled files never store raw secrets.
    body_with_marker = sanitize_text(body_with_marker)[0]
    spilled, spill_ref = apply_spill_policy(
        body_with_marker,
        session_id=str(session_id),
        tool_name="bash",
        max_inline_bytes=MAX_OUTPUT_CHARS,
        suggested_name="bash.txt",
    )
    if spill_ref is not None:
        truncated_text, is_truncated = spilled, True
    else:
        truncated_text, is_truncated = _truncate_output(body_with_marker)

    if cwd:
        try:
            resolve_exec_cwd(cwd, str(project_root), isolated)
        except (ValueError, OSError):
            cwd = None
    _update_session_cwd(session_id, start_cwd, cwd)

    ok = (
        execution["exit_code"] == 0
        and execution["signal"] is None
        and not execution["timed_out"]
        and not execution.get("error")
    )
    error_msg = None
    if not ok:
        error_msg = _build_error_message(
            execution["exit_code"],
            execution["signal"],
            execution.get("error"),
            execution["timed_out"],
        )

    metadata: dict[str, Any] = {
        "exitCode": execution["exit_code"],
        "signal": execution["signal"],
        "cwd": cwd,
        "truncated": is_truncated,
        "shellPath": shell_path,
        "startCwd": start_cwd,
        "timedOut": execution["timed_out"],
        "timeoutMs": execution["timeout_ms"],
    }
    if spill_ref is not None:
        metadata["spill"] = spill_ref.to_dict()
    if execution.get("sandbox"):
        metadata["sandbox"] = execution["sandbox"]
    if execution.get("deadline_at_ms"):
        import datetime

        metadata["deadlineAt"] = datetime.datetime.fromtimestamp(
            execution["deadline_at_ms"] / 1000.0, datetime.timezone.utc
        ).isoformat()

    return ToolResult(
        ok=ok,
        name="bash",
        output=truncated_text if truncated_text else None,
        error=error_msg,
        metadata=metadata,
    )


def _execute_shell_command(
    shell_path: str,
    shell_args: list[str],
    cwd: str,
    command: str,
    context: Any,
    mode_override: Any = None,
) -> dict[str, Any]:
    configured_env: dict[str, str] = {}
    if isinstance(context, dict):
        configured_env = dict(context.get("shell_env") or {})
    elif hasattr(context, "shell_env") and context.shell_env:
        configured_env = dict(context.shell_env)

    bash_timeout_ms = getattr(context, "bash_timeout_ms", None)
    bash_min_timeout_ms = getattr(context, "bash_min_timeout_ms", None)
    if isinstance(context, dict):
        bash_timeout_ms = context.get("bash_timeout_ms", bash_timeout_ms)
        bash_min_timeout_ms = context.get("bash_min_timeout_ms", bash_min_timeout_ms)

    initial_timeout_ms = clamp_bash_timeout_ms(
        bash_timeout_ms if bash_timeout_ms is not None else DEFAULT_BASH_TIMEOUT_MS,
        bash_min_timeout_ms,
    )

    started_at_ms = int(time.time() * 1000)
    state = {
        "timeout_ms": initial_timeout_ms,
        "deadline_at_ms": started_at_ms + initial_timeout_ms,
        "timed_out": False,
        "settled": False,
    }

    env = build_shell_env(shell_path, configured_env)
    kwargs: dict[str, Any] = {
        "cwd": cwd,
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "errors": "replace",
        "bufsize": 1,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    try:
        argv, sandbox_meta = _sandbox_wrap(shell_path, shell_args, context, cwd, mode_override)
    except SandboxUnavailableError as sb_err:
        # Fail-closed: never run the command without the requested OS sandbox.
        return {
            "stdout": "",
            "stderr": "",
            "exitCode": None,
            "exit_code": None,
            "signal": None,
            "error": str(sb_err),
            "timed_out": False,
            "timeout_ms": state["timeout_ms"],
            "deadline_at_ms": state["deadline_at_ms"],
            "sandbox": {"sandboxApplied": False, "sandboxDenied": str(sb_err)},
            "sandbox_denied": True,
            "sandbox_mode": mode_override if isinstance(mode_override, str) else None,
        }
    try:
        proc = subprocess.Popen(argv, **kwargs)
    except Exception as spawn_err:
        return {
            "stdout": "",
            "stderr": "",
            "exitCode": None,
            "exit_code": None,
            "signal": None,
            "error": str(spawn_err),
            "timed_out": False,
            "timeout_ms": state["timeout_ms"],
            "deadline_at_ms": state["deadline_at_ms"],
            "sandbox": sandbox_meta,
        }

    pid = proc.pid
    on_process_start = getattr(context, "on_process_start", None) or (
        context.get("on_process_start") if isinstance(context, dict) else None
    )
    on_process_exit = getattr(context, "on_process_exit", None) or (
        context.get("on_process_exit") if isinstance(context, dict) else None
    )
    on_process_stdout = getattr(context, "on_process_stdout", None) or (
        context.get("on_process_stdout") if isinstance(context, dict) else None
    )
    on_process_timeout_control = getattr(context, "on_process_timeout_control", None) or (
        context.get("on_process_timeout_control") if isinstance(context, dict) else None
    )

    if on_process_start and pid:
        on_process_start(pid, command)

    timer_lock = threading.Lock()
    active_timer: list[threading.Timer | None] = [None]

    def get_timeout_info() -> ProcessTimeoutInfo:
        with timer_lock:
            return ProcessTimeoutInfo(
                timeout_ms=int(state["timeout_ms"]),
                started_at_ms=started_at_ms,
                deadline_at_ms=int(state["deadline_at_ms"]),
                timed_out=bool(state["timed_out"]),
            )

    def trigger_timeout() -> None:
        with timer_lock:
            if state["settled"] or state["timed_out"] or not pid:
                return
            state["timed_out"] = True
        kill_process_tree(pid)

    def schedule_timeout() -> None:
        with timer_lock:
            if active_timer[0]:
                active_timer[0].cancel()
                active_timer[0] = None
            if state["settled"]:
                return
            remaining_s = max(0.0, (state["deadline_at_ms"] - int(time.time() * 1000)) / 1000.0)
            t = threading.Timer(remaining_s, trigger_timeout)
            t.daemon = True
            active_timer[0] = t
            t.start()

    def set_timeout_ms(next_timeout_ms: int) -> ProcessTimeoutInfo:
        clamped = clamp_bash_timeout_ms(next_timeout_ms, bash_min_timeout_ms)
        with timer_lock:
            state["timeout_ms"] = clamped
            state["deadline_at_ms"] = started_at_ms + clamped
        schedule_timeout()
        return get_timeout_info()

    timeout_control = ProcessTimeoutControl(
        get_info=get_timeout_info,
        set_timeout_ms=set_timeout_ms,
    )

    if on_process_timeout_control and pid:
        on_process_timeout_control(pid, timeout_control)

    schedule_timeout()

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    def reader(stream: Any, chunk_list: list[str]) -> None:
        try:
            for line in iter(stream.readline, ""):
                if not line:
                    break
                chunk_list.append(line)
                if on_process_stdout and pid:
                    on_process_stdout(pid, line)
            stream.close()
        except Exception:
            pass

    t_out = threading.Thread(target=reader, args=(proc.stdout, stdout_chunks), daemon=True)
    t_err = threading.Thread(target=reader, args=(proc.stderr, stderr_chunks), daemon=True)
    t_out.start()
    t_err.start()

    proc.wait()
    t_out.join(timeout=2.0)
    t_err.join(timeout=2.0)

    with timer_lock:
        state["settled"] = True
        if active_timer[0]:
            active_timer[0].cancel()
            active_timer[0] = None

    if on_process_timeout_control and pid:
        on_process_timeout_control(pid, None)
    if on_process_exit and pid:
        on_process_exit(pid)

    exit_code = proc.returncode
    signal_name = None
    if exit_code and exit_code < 0:
        signal_name = f"SIG{-exit_code}"
        exit_code = None

    return {
        "stdout": "".join(stdout_chunks),
        "stderr": "".join(stderr_chunks),
        "exit_code": exit_code,
        "signal": signal_name,
        "error": None,
        "timed_out": state["timed_out"],
        "timeout_ms": state["timeout_ms"],
        "deadline_at_ms": state["deadline_at_ms"],
        "sandbox": sandbox_meta,
    }


def _start_background_shell_command(
    shell_path: str,
    shell_args: list[str],
    cwd: str,
    command: str,
    marker: str,
    context: Any,
    mode_override: Any = None,
) -> ToolResult:
    BACKGROUND_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    task_id = f"bash-{uuid.uuid4()}"
    output_path = BACKGROUND_OUTPUT_DIR / f"{task_id}.log"
    output_path.touch(exist_ok=True)
    started_at_ms = int(time.time() * 1000)

    configured_env: dict[str, str] = {}
    if isinstance(context, dict):
        configured_env = dict(context.get("shell_env") or {})
    elif hasattr(context, "shell_env") and context.shell_env:
        configured_env = dict(context.shell_env)

    env = build_shell_env(shell_path, configured_env)
    kwargs: dict[str, Any] = {
        "cwd": cwd,
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "errors": "replace",
        "bufsize": 1,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    try:
        argv, sandbox_meta = _sandbox_wrap(shell_path, shell_args, context, cwd, mode_override)
    except SandboxUnavailableError as sb_err:
        # Fail-closed: never run the command without the requested OS sandbox.
        return ToolResult(
            ok=False,
            name="bash",
            error=str(sb_err),
            metadata={"sandbox": {"sandboxApplied": False, "sandboxDenied": str(sb_err)}},
        )
    try:
        proc = subprocess.Popen(argv, **kwargs)
    except Exception as e:
        return ToolResult(
            ok=False,
            name="bash",
            error=f"Failed to start background command: {e}",
            metadata={"sandbox": sandbox_meta},
        )

    pid = proc.pid
    stop_command = _build_stop_command(pid) if pid > 0 else None

    on_process_start = getattr(context, "on_process_start", None) or (
        context.get("on_process_start") if isinstance(context, dict) else None
    )
    on_process_exit = getattr(context, "on_process_exit", None) or (
        context.get("on_process_exit") if isinstance(context, dict) else None
    )
    on_process_stdout = getattr(context, "on_process_stdout", None) or (
        context.get("on_process_stdout") if isinstance(context, dict) else None
    )
    on_background_process_complete = getattr(context, "on_background_process_complete", None) or (
        context.get("on_background_process_complete") if isinstance(context, dict) else None
    )
    session_id = getattr(context, "session_id", "default") or (
        context.get("session_id", "default") if isinstance(context, dict) else "default"
    )

    if on_process_start and pid:
        on_process_start(pid, command)

    try:
        get_job_store().start(
            job_id=task_id,
            session_id=str(session_id),
            kind="bash",
            label=command,
            process_id=pid if pid else None,
            output_path=str(output_path),
        )
    except RuntimeError as exc:
        # Job cap rejection: do not orphan the just-spawned process.
        if pid:
            try:
                kill_process_tree(int(pid))
            except Exception:
                pass
        return ToolResult(ok=False, name="bash", error=str(exc))

    # Background worker thread to stream output to file and notify completion
    def bg_worker() -> None:
        stdout_captured = ""
        stderr_captured = ""

        def append_output_file(text: str) -> None:
            try:
                with open(output_path, "a", encoding="utf-8") as f:
                    f.write(text)
            except Exception:
                pass

        def read_pipe(stream: Any, is_stderr: bool) -> None:
            nonlocal stdout_captured, stderr_captured
            try:
                for line in iter(stream.readline, ""):
                    if not line:
                        break
                    if is_stderr:
                        stderr_captured = _append_chunk(stderr_captured, line)
                    else:
                        stdout_captured = _append_chunk(stdout_captured, line)
                    append_output_file(line)
                    if on_process_stdout and pid:
                        on_process_stdout(pid, line)
                stream.close()
            except Exception:
                pass

        t1 = threading.Thread(target=read_pipe, args=(proc.stdout, False), daemon=True)
        t2 = threading.Thread(target=read_pipe, args=(proc.stderr, True), daemon=True)
        t1.start()
        t2.start()

        proc.wait()
        t1.join(timeout=2.0)
        t2.join(timeout=2.0)

        cleaned_stdout, next_cwd = _strip_marker(stdout_captured, marker)
        final_output = _join_output(cleaned_stdout, stderr_captured)

        # Overwrite file with marker stripped final output
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(final_output)
        except Exception:
            pass

        _update_session_cwd(session_id, cwd, next_cwd)

        if on_process_exit and pid:
            on_process_exit(pid)

        exit_code = proc.returncode
        signal_name = None
        if exit_code and exit_code < 0:
            signal_name = f"SIG{-exit_code}"
            exit_code = None

        ok = exit_code == 0 and signal_name is None
        err_msg = None if ok else _build_error_message(exit_code, signal_name)

        get_job_store().complete(
            task_id,
            ok=ok,
            exit_code=exit_code,
            signal=signal_name,
            detail=err_msg,
        )

        if on_background_process_complete:
            on_background_process_complete(
                BackgroundProcessCompletion(
                    task_id=task_id,
                    process_id=pid,
                    command=command,
                    output_path=str(output_path),
                    ok=ok,
                    exit_code=exit_code,
                    signal=signal_name,
                    error=err_msg,
                    cwd=next_cwd or cwd,
                    shell_path=shell_path,
                    started_at_ms=started_at_ms,
                    completed_at_ms=int(time.time() * 1000),
                )
            )

    threading.Thread(target=bg_worker, daemon=True).start()

    parts = [f"Command running in background with ID: {task_id}."]
    if stop_command:
        parts.append(f"Stop it with job_kill (job_id={task_id}) or: {stop_command}")
    else:
        parts.append(f"Stop it with job_kill using job_id {task_id}.")
    parts.append(
        "Read output with job_output. Do not busy-poll; keep working on independent steps."
    )
    parts.append(f"Output is being written to: {output_path}")

    return ToolResult(
        ok=True,
        name="bash",
        output=" ".join(parts),
        metadata={
            "backgroundTaskId": task_id,
            "processId": pid,
            "outputPath": str(output_path),
            "stopCommand": stop_command,
            "cwd": cwd,
            "shellPath": shell_path,
            "startCwd": cwd,
            "runInBackground": True,
        },
    )


# --- from coderai/core/tools/pwsh.py ---
"""PowerShell / pwsh tool — cross-platform PowerShell execution with timeout and background support."""


import asyncio
import pathlib
import shutil
import tempfile

from coderai.sandbox import delete_seatbelt_profile

MAX_OUTPUT_CHARS = 30000
DEFAULT_PWSH_TIMEOUT_S = 120.0


def _resolve_pwsh_executable() -> str | None:
    """Find the best available PowerShell executable on the host."""
    candidates = ["pwsh", "powershell.exe", "powershell"]
    for c in candidates:
        found = shutil.which(c)
        if found:
            return found
    return None


async def handle_pwsh_tool(args: dict[str, Any], context: Any) -> ToolResult:
    """Execute a PowerShell command or script."""
    command = as_str(args.get("command", "")).strip()
    if not command:
        return ToolResult(
            ok=False,
            name="pwsh",
            error="Missing required argument 'command'.",
        )

    description = as_str(args.get("description", "")).strip() or command[:50]
    run_in_background = bool(args.get("run_in_background", False))
    project_root = getattr(context, "project_root", ".") if context else "."
    session_id = getattr(context, "session_id", "default") if context else "default"
    sandbox_mode = getattr(context, "sandbox_mode", None)
    if isinstance(context, dict):
        sandbox_mode = context.get("sandbox_mode", sandbox_mode)
        project_root = context.get("project_root", project_root)

    pwsh_bin = _resolve_pwsh_executable()
    if not pwsh_bin:
        # Fallback error if no powershell is installed on non-Windows
        if sys.platform != "win32":
            return ToolResult(
                ok=False,
                name="pwsh",
                error="PowerShell ('pwsh') is not installed on this system. Please install PowerShell or use the 'bash' tool.",
            )
        pwsh_bin = "powershell.exe"

    redirect_error = _reject_escaping_redirects(
        command,
        str(project_root),
        str(project_root),
        _isolated_root(context),
        sandbox_mode,
        tool_name="pwsh",
    )
    if redirect_error is not None:
        return redirect_error

    cmd_argv = [pwsh_bin, "-NoProfile", "-NonInteractive", "-Command", command]
    try:
        wrapped_argv, sandbox_meta = wrap_sandbox_command(
            cmd_argv,
            mode=sandbox_mode,
            workspace_root=str(project_root),
            cwd=str(project_root),
        )
    except SandboxUnavailableError as sb_err:
        # Fail-closed: never run the command without the requested OS sandbox.
        return ToolResult(
            ok=False,
            name="pwsh",
            error=str(sb_err),
            metadata={"sandbox": {"sandboxApplied": False, "sandboxDenied": str(sb_err)}},
        )

    if run_in_background:
        # Background job execution
        job_id = f"job_pwsh_{uuid.uuid4().hex[:8]}"
        log_dir = pathlib.Path(tempfile.gettempdir()) / "coderai-pwsh"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{job_id}.log"

        f = open(log_file, "w", encoding="utf-8")
        proc = subprocess.Popen(
            wrapped_argv,
            cwd=project_root,
            stdout=f,
            stderr=subprocess.STDOUT,
            text=True,
        )

        job_store = get_job_store()
        job_store.start(
            job_id=job_id,
            session_id=session_id,
            kind="pwsh",
            label=description,
            output_path=str(log_file),
            process_id=proc.pid,
        )

        meta: dict[str, Any] = {"job_id": job_id, "pid": proc.pid, "kind": "pwsh"}
        if sandbox_meta:
            meta["sandbox"] = sandbox_meta

        return ToolResult(
            ok=True,
            name="pwsh",
            output=(
                f"Started background PowerShell job '{job_id}' (PID {proc.pid}).\n"
                f"Description: {description}\n"
                f"Use `job_output(job_id='{job_id}')` to stream logs or `job_kill(job_id='{job_id}')` to terminate."
            ),
            metadata=meta,
        )

    # Synchronous execution offloaded to worker thread to prevent event loop starvation
    start_time = time.time()
    profile_path = sandbox_meta.get("sandboxProfile")
    try:
        completed = await asyncio.to_thread(
            subprocess.run,
            wrapped_argv,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=DEFAULT_PWSH_TIMEOUT_S,
        )

        combined_output: str = completed.stdout or ""
        if completed.stderr:
            if combined_output:
                combined_output += "\n"
            combined_output += completed.stderr

        duration = max(0.0, time.time() - start_time)

        # Sanitize BEFORE disk spill so spilled files never store raw secrets.
        combined_output = sanitize_text(combined_output)[0]
        # Apply spill policy if output is large
        output_text, _ = apply_spill_policy(
            combined_output,
            session_id=session_id,
            tool_name="pwsh",
        )

        meta = {"returncode": completed.returncode, "duration_seconds": duration}
        if sandbox_meta:
            meta["sandbox"] = sandbox_meta

        if completed.returncode != 0:
            return ToolResult(
                ok=False,
                name="pwsh",
                output=output_text or f"Command failed with exit code {completed.returncode}.",
                error=f"PowerShell command exited with code {completed.returncode}.",
                metadata=meta,
            )

        return ToolResult(
            ok=True,
            name="pwsh",
            output=output_text or "(Command executed with no output)",
            metadata=meta,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(
            ok=False,
            name="pwsh",
            error=f"PowerShell command timed out after {DEFAULT_PWSH_TIMEOUT_S}s.",
        )
    except Exception as exc:
        return ToolResult(
            ok=False,
            name="pwsh",
            error=f"Failed to execute PowerShell command: {exc}",
        )
    finally:
        if profile_path:
            delete_seatbelt_profile(profile_path)


# --- Params and Shell callable tool ---
from typing import Self
from pydantic import BaseModel, Field, model_validator
from kosong.tooling import CallableTool2, ToolReturnValue
from coderai.tools.utils import ToolResultBuilder
from coderai.tools.display import ShellDisplayBlock

MAX_FOREGROUND_TIMEOUT = 5 * 60
MAX_BACKGROUND_TIMEOUT = 24 * 60 * 60


class Params(BaseModel):
    command: str = Field(description="The command to execute.")
    timeout: int = Field(
        description=(
            "The timeout in seconds for the command to execute. "
            "If the command takes longer than this, it will be killed."
        ),
        default=60,
        ge=1,
        le=MAX_BACKGROUND_TIMEOUT,
    )
    run_in_background: bool = Field(
        default=False,
        description="Whether to run the command as a background task.",
    )
    description: str = Field(
        default="",
        description=(
            "A short description for the background task. Required when run_in_background=true."
        ),
    )

    @model_validator(mode="after")
    def _validate_background_fields(self) -> Self:
        if self.run_in_background and not self.description.strip():
            raise ValueError("description is required when run_in_background is true")
        if not self.run_in_background and self.timeout > MAX_FOREGROUND_TIMEOUT:
            raise ValueError(
                f"timeout must be <= {MAX_FOREGROUND_TIMEOUT}s for foreground commands; "
                f"use run_in_background=true for longer timeouts (up to {MAX_BACKGROUND_TIMEOUT}s)"
            )
        return self


class Shell(CallableTool2[Params]):
    name: str = "Shell"
    params: type[Params] = Params

    def __init__(
        self,
        approval: Any = None,
        environment: Any = None,
        runtime: Any = None,
        description: str = "Run shell commands.",
    ):
        super().__init__(
            description=description,
        )
        self._approval = approval
        self._environment = environment
        self._runtime = runtime

    async def __call__(self, params: Params) -> ToolReturnValue:
        builder = ToolResultBuilder()
        if not params.command:
            return builder.error("Command cannot be empty.", brief="Empty command")

        if self._approval is not None and hasattr(self._approval, "request"):
            approval_result = await self._approval.request(
                self.name,
                "run command",
                f"Run command `{params.command}`",
                display=[ShellDisplayBlock(language="bash", command=params.command)],
            )
            if not approval_result:
                return approval_result.rejection_error()

        def _exec_bash(cmd: str, timeout_ms: int, run_in_background: bool, description: str) -> Any:
            return handle_bash_tool(
                {
                    "command": cmd,
                    "timeout_ms": timeout_ms,
                    "run_in_background": run_in_background,
                    "description": description,
                },
                self._runtime,
            )

        res = await asyncio.to_thread(
            _exec_bash,
            params.command,
            timeout_ms=params.timeout * 1000,
            run_in_background=params.run_in_background,
            description=params.description,
        )
        if not res.ok:
            builder.write(res.output or "")
            return builder.error(
                res.error or "Command failed", brief=f"Failed: {params.command[:40]}"
            )
        builder.write(res.output or "")
        return builder.ok("Command executed successfully.")
