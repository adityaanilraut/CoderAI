"""Terminal tools for command execution."""

import asyncio
import logging
import re
import shlex
import shutil
import atexit
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from .base import Tool
from ..config import config_manager

logger = logging.getLogger(__name__)

# Commands that are blocked by default for safety.
#
# NOTE: Blocklists are a *speed bump*, not real security — the actual safety
# comes from ``requires_confirmation`` plus the approval UX. We match against
# normalised tokens so things like ``echo "$(date)"`` are no longer blocked
# purely because they contain a ``$(`` substring.
#
# The shell-wrapper forms (``bash -c``, ``sh -c``, ``zsh -c``) are NOT in
# this list — ``_extract_inner_command`` peels them off and re-checks the
# inner command.
BLOCKED_PATTERNS = [
    "rm -rf /",
    "rm -rf ~",
    "rm -rf /*",
    "mkfs",
    "dd if=",
    ":(){:|:&};:",       # fork bomb
    "> /dev/sda",
    "chmod -R 777 /",
    "shutdown",
    "reboot",
    "halt",
    "format c:",
    "base64 -d",
    "base64 --decode",
]

# Patterns that indicate piping a network fetch straight into a shell.
# Matched against a whitespace-normalised lowercase command.
_PIPE_TO_SHELL_RE = re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sh|bash|zsh|fish|python[23]?|node)\b")

# Commands that require user confirmation
DANGEROUS_PREFIXES = [
    "rm ",
    "rmdir ",
    "sudo ",
    "chmod ",
    "chown ",
    "mv /",
    "dd ",
    "kill ",
    "killall ",
    "pkill ",
    "pip install",
    "pip uninstall",
    "npm install",
    "npm uninstall",
    "apt ",
    "apt-get ",
    "brew ",
    "curl ",
    "wget ",
    "docker rm",
    "docker rmi",
]

# Common command aliases: (original, replacement)
# Applied when the original is not found on PATH but the replacement is.
_COMMAND_ALIASES = [
    ("python", "python3"),
    ("pip", "pip3"),
]


def _normalize_command(command: str) -> str:
    """Normalize command for safety checks: strip, collapse whitespace, lowercase."""
    return re.sub(r'\s+', ' ', command.strip()).lower()


# Shells that can wrap arbitrary commands — we extract the inner command
# and re-check it against the blocklist/dangerous prefixes.
_SHELL_WRAPPERS = ("bash -c ", "sh -c ", "zsh -c ", "/bin/bash -c ", "/bin/sh -c ", "/bin/zsh -c ")


def _extract_inner_command(cmd_lower: str) -> Optional[str]:
    """If the command invokes a shell wrapper, extract and return the inner command."""
    for prefix in _SHELL_WRAPPERS:
        if cmd_lower.startswith(prefix):
            inner = cmd_lower[len(prefix):].strip()
            # Strip surrounding quotes if present
            if len(inner) >= 2 and inner[0] in ('"', "'") and inner[-1] == inner[0]:
                inner = inner[1:-1]
            return inner
    return None


def is_command_blocked(command: str) -> bool:
    """Check if a command is in the blocklist.

    Normalizes whitespace, lowercases, and recursively checks commands
    wrapped in shell invocations like ``bash -c '...'``.
    """
    cmd_lower = _normalize_command(command)

    if any(blocked in cmd_lower for blocked in BLOCKED_PATTERNS):
        return True

    if _PIPE_TO_SHELL_RE.search(cmd_lower):
        return True

    # Check inner command for shell wrappers
    inner = _extract_inner_command(cmd_lower)
    if inner is not None:
        return is_command_blocked(inner)

    return False


def is_command_dangerous(command: str) -> bool:
    """Check if a command should require confirmation."""
    cmd_lower = _normalize_command(command)

    if any(cmd_lower.startswith(prefix) for prefix in DANGEROUS_PREFIXES):
        return True

    # Also check inner command of shell wrappers
    inner = _extract_inner_command(cmd_lower)
    if inner is not None:
        return is_command_dangerous(inner)

    return False


def _rewrite_command_aliases(command: str) -> str:
    """Rewrite common command aliases when the original isn't on PATH.

    For example, on macOS and newer Linux distros ``python`` often doesn't
    exist but ``python3`` does.  This helper transparently rewrites the
    command so that the LLM-generated commands work out of the box.
    """
    for original, replacement in _COMMAND_ALIASES:
        # Only rewrite if the command *starts* with the original token
        # (e.g. "python foo.py", "python -m pytest") and `original` is
        # missing from PATH while `replacement` is available.
        if command == original or command.startswith(original + " "):
            if shutil.which(original) is None and shutil.which(replacement) is not None:
                command = replacement + command[len(original):]
                logger.debug(f"Rewrote '{original}' → '{replacement}' in command")
                break
    return command


class RunCommandParams(BaseModel):
    command: str = Field(..., description="Shell command to execute")
    working_dir: str = Field(".", description="Working directory for the command (default: current)")
    timeout: int = Field(60, description="Timeout in seconds (default: 60)")


class RunCommandTool(Tool):
    """Tool for executing shell commands with safety checks."""

    name = "run_command"
    description = "Execute a shell command and return its output. Dangerous commands require confirmation."
    parameters_model = RunCommandParams
    requires_confirmation = True

    async def execute(
        self, command: str, working_dir: str = ".", timeout: int = 60
    ) -> Dict[str, Any]:
        """Execute shell command with safety checks.

        Uses shlex.split + create_subprocess_exec for simple commands,
        falls back to create_subprocess_shell for complex shell syntax.
        """
        try:
            # Block known destructive commands (these are never allowed)
            if is_command_blocked(command):
                return {
                    "success": False,
                    "error": f"Command blocked for safety: {command}",
                    "error_code": "blocked",
                    "blocked": True,
                }

            # Block interactive commands that would hang without a TTY
            from ..safeguards import is_interactive_command
            if is_interactive_command(command):
                logger.warning(f"Blocked interactive command: {command}")
                return {
                    "success": False,
                    "error": (
                        "Command appears interactive (requires TTY/user input): "
                        f"{command!r}. Use an interactive terminal session instead."
                    ),
                    "error_code": "interactive",
                    "interactive": True,
                }

            # Log dangerous commands (actual confirmation is handled by
            # requires_confirmation + the confirmation callback in ToolRegistry)
            if is_command_dangerous(command):
                logger.warning(f"Executing potentially dangerous command: {command}")

            # Rewrite common aliases (e.g. python -> python3 on macOS)
            command = _rewrite_command_aliases(command)

            # Try to use exec (no shell) for simple commands
            needs_shell = any(c in command for c in ['|', '>', '<', '&&', '||', ';', '`', '$', '*', '?', '~', '&'])

            if needs_shell:
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=working_dir,
                )
            else:
                try:
                    args = shlex.split(command)
                    process = await asyncio.create_subprocess_exec(
                        *args,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=working_dir,
                    )
                except ValueError:
                    # shlex.split can fail on malformed input — fallback to shell
                    process = await asyncio.create_subprocess_shell(
                        command,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=working_dir,
                    )

            # Wait for completion with timeout
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=timeout
                )
                if process.returncode is None:
                    await process.wait()
            except asyncio.TimeoutError:
                process.kill()
                return {
                    "success": False,
                    "error": f"Command timed out after {timeout} seconds",
                    "error_code": "timeout",
                }

            # Truncate very large output to prevent context overflow
            stdout_str = stdout.decode("utf-8", errors="replace")
            stderr_str = stderr.decode("utf-8", errors="replace")

            max_output = config_manager.load().max_command_output
            if len(stdout_str) > max_output:
                stdout_str = (
                    stdout_str[:max_output // 2]
                    + f"\n\n... [truncated {len(stdout_str) - max_output} chars] ...\n\n"
                    + stdout_str[-max_output // 2:]
                )
            if len(stderr_str) > max_output:
                stderr_str = stderr_str[:max_output]

            return {
                "success": process.returncode == 0,
                "returncode": process.returncode,
                "stdout": stdout_str,
                "stderr": stderr_str,
                "command": command,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


class RunBackgroundParams(BaseModel):
    command: str = Field(..., description="Shell command to execute in background")
    working_dir: str = Field(".", description="Working directory for the command (default: current)")


class RunBackgroundTool(Tool):
    """Tool for starting background processes."""

    name = "run_background"
    description = "Start a command in the background (for long-running processes like servers)"
    parameters_model = RunBackgroundParams
    requires_confirmation = True

    # Track spawned background processes for cleanup / status queries
    # NOTE: This is now per-instance; a module-level set tracks all instances
    # so atexit cleanup still works.
    _all_instances = set()

    def __init__(self):
        super().__init__()
        self._processes: Dict[int, asyncio.subprocess.Process] = {}
        RunBackgroundTool._all_instances.add(self)

    async def execute(self, command: str, working_dir: str = ".") -> Dict[str, Any]:
        """Start background process with tracking.

        Uses the same exec-vs-shell heuristic as ``run_command`` — plain
        commands go through ``create_subprocess_exec`` (no shell interpretation),
        only commands with shell metacharacters fall back to the shell.
        """
        try:
            if is_command_blocked(command):
                return {
                    "success": False,
                    "error": f"Command blocked for safety: {command}",
                    "blocked": True,
                }

            if is_command_dangerous(command):
                logger.warning(f"Executing potentially dangerous background command: {command}")

            command = _rewrite_command_aliases(command)
            needs_shell = any(c in command for c in ['|', '>', '<', '&&', '||', ';', '`', '$', '*', '?', '~', '&'])

            if needs_shell:
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    cwd=working_dir,
                )
            else:
                try:
                    args = shlex.split(command)
                    process = await asyncio.create_subprocess_exec(
                        *args,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                        cwd=working_dir,
                    )
                except ValueError:
                    process = await asyncio.create_subprocess_shell(
                        command,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                        cwd=working_dir,
                    )

            # Track the process
            self._processes[process.pid] = process

            return {
                "success": True,
                "pid": process.pid,
                "command": command,
                "message": "Process started in background",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_tracked_processes(self) -> Dict[int, asyncio.subprocess.Process]:
        """Get all tracked background processes for this instance."""
        return self._processes

    def cleanup_finished(self) -> int:
        """Clean up finished processes from tracking. Returns count removed."""
        finished = [pid for pid, proc in self._processes.items() if proc.returncode is not None]
        for pid in finished:
            del self._processes[pid]
        return len(finished)

    def terminate_all(self) -> int:
        """Forcefully terminate all remaining tracked processes."""
        terminated = 0
        for pid, proc in dict(self._processes).items():
            if proc.returncode is None:
                try:
                    proc.kill()
                    terminated += 1
                except Exception:
                    pass
        self._processes.clear()
        return terminated


def _cleanup_all_background():
    """Terminate background processes from ALL RunBackgroundTool instances."""
    for instance in list(RunBackgroundTool._all_instances):
        instance.terminate_all()

# Register cleanup on exit to prevent process leaks
atexit.register(_cleanup_all_background)


# ---------------------------------------------------------------------------
# Process management: list background processes and kill by PID
# ---------------------------------------------------------------------------


class ListProcessesParams(BaseModel):
    pass


class ListProcessesTool(Tool):
    """List all background processes started by run_background."""

    name = "list_processes"
    description = (
        "List all background processes currently tracked by the agent "
        "(started via run_background). Shows PID, command, and running status."
    )
    category = "shell"
    parameters_model = ListProcessesParams
    is_read_only = True

    async def execute(self) -> Dict[str, Any]:
        try:
            processes = []
            for instance in RunBackgroundTool._all_instances:
                for pid, proc in instance._processes.items():
                    processes.append(
                        {
                            "pid": pid,
                            "running": proc.returncode is None,
                            "returncode": proc.returncode,
                        }
                    )
            return {
                "success": True,
                "processes": processes,
                "count": len(processes),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


class KillProcessParams(BaseModel):
    pid: int = Field(..., description="Process ID (PID) to terminate")
    force: bool = Field(False, description="Send SIGKILL instead of SIGTERM (force kill)")


class KillProcessTool(Tool):
    """Terminate a background process by PID."""

    name = "kill_process"
    description = (
        "Terminate a background process that was started with run_background. "
        "Sends SIGTERM by default; use force=true for SIGKILL."
    )
    category = "shell"
    parameters_model = KillProcessParams
    requires_confirmation = True

    async def execute(self, pid: int, force: bool = False) -> Dict[str, Any]:
        import signal as _signal

        try:
            found = False
            for instance in RunBackgroundTool._all_instances:
                proc = instance._processes.get(pid)
                if proc is not None:
                    found = True
                    if proc.returncode is not None:
                        return {
                            "success": False,
                            "error": f"Process {pid} has already exited (returncode={proc.returncode}).",
                        }
                    sig = _signal.SIGKILL if force else _signal.SIGTERM
                    proc.send_signal(sig)
                    del instance._processes[pid]
                    return {
                        "success": True,
                        "pid": pid,
                        "signal": "SIGKILL" if force else "SIGTERM",
                        "message": f"Sent {'SIGKILL' if force else 'SIGTERM'} to process {pid}.",
                    }

            if not found:
                # Also try OS-level kill for PIDs not tracked in our registry
                import os as _os
                try:
                    sig = _signal.SIGKILL if force else _signal.SIGTERM
                    _os.kill(pid, sig)
                    return {
                        "success": True,
                        "pid": pid,
                        "signal": "SIGKILL" if force else "SIGTERM",
                        "message": f"Sent signal to untracked process {pid}.",
                        "warning": "Process was not in the tracked process list.",
                    }
                except ProcessLookupError:
                    return {"success": False, "error": f"No process found with PID {pid}."}
                except PermissionError:
                    return {"success": False, "error": f"Permission denied sending signal to PID {pid}."}
        except Exception as e:
            return {"success": False, "error": str(e)}
