"""PowerShell / pwsh tool — cross-platform PowerShell execution with timeout and background support."""

from __future__ import annotations

import asyncio
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Any

from coderai.core.jobs import get_job_store
from coderai.core.sandbox import delete_seatbelt_profile, wrap_sandbox_command
from coderai.core.spill import apply_spill_policy
from coderai.core.tools.types import ToolResult, as_str

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

    cmd_argv = [pwsh_bin, "-NoProfile", "-NonInteractive", "-Command", command]
    wrapped_argv, sandbox_meta = wrap_sandbox_command(
        cmd_argv,
        mode=sandbox_mode,
        workspace_root=str(project_root),
        cwd=str(project_root),
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

