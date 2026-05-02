"""Terminal tools for command execution."""

import asyncio
import logging
import os
import re
import shlex
import shutil
import signal as _signal
from pathlib import Path
from typing import Any, Dict, List, Optional

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
    "rm -r -f /",
    "rm -r -f ~",
    "rm -r -f /*",
    "rm -rf --no-preserve-root /",
    "mkfs",
    "/sbin/mkfs",
    "mkfs.",
    "dd if=/",
    "dd if ~",
    ":(){:|:&};:",       # fork bomb
    "> /dev/sda",
    "> /dev/sdb",
    "> /dev/hda",
    "chmod -R 777 /",
    "chmod -R 777 /*",
    "chmod 777 /",
    "shutdown",
    "/sbin/shutdown",
    "systemctl poweroff",
    "reboot",
    "systemctl reboot",
    "halt",
    "base64 -d",
    "base64 --decode",
    "nc -e",
    "bash -i >&",
]

_RM_DESTRUCTIVE_REGEX = re.compile(r'\brm\s+.*(?:-r|-f|--recursive|--force).*(?:/|~)\b')

def _build_blocked_regexes(patterns):
    """Precompile blocked-pattern regexes with token-boundary matching.

    The boundary anchors prevent a pattern like ``"rm -rf /"`` from matching
    against ``"rm -rf /tmp/build"`` while still catching the bare form.
    """
    return [
        re.compile(r'(?:^|\s)' + re.escape(p) + r'(?:\s|$)')
        for p in patterns
    ]

_BLOCKED_REGEXES = _build_blocked_regexes(BLOCKED_PATTERNS)

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


_SHELL_METACHARS = re.compile(r'[|><&;$*?~`\\]')


def _needs_shell(command: str) -> bool:
    """Check whether *command* requires a shell to interpret metacharacters.

    Characters inside single-quoted strings do NOT trigger shell mode (e.g.
    ``echo '$HOME'`` runs fine with ``create_subprocess_exec``).
    """
    # Strip single-quoted segments, then check the remainder
    stripped = re.sub(r"'[^']*'", "", command)
    return bool(_SHELL_METACHARS.search(stripped))


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

    if any(r.search(cmd_lower) for r in _BLOCKED_REGEXES):
        return True

    if _RM_DESTRUCTIVE_REGEX.search(cmd_lower):
        return True

    if _PIPE_TO_SHELL_RE.search(cmd_lower):
        return True

    # Check inner command for shell wrappers
    inner = _extract_inner_command(cmd_lower)
    if inner is not None:
        return is_command_blocked(inner)

    return False


def _resolve_working_dir(working_dir: str) -> "tuple[Optional[Path], Optional[str]]":
    """Resolve *working_dir* against the project root, rejecting escapes.

    By default the terminal tools may not ``cd`` outside the project: a
    mis-quoted path in a tool-generated command should not end up running
    ``rm``-ish things in the user's home directory. Set
    ``CODERAI_ALLOW_OUTSIDE_PROJECT=1`` to opt out when you genuinely need
    cross-repo access.

    Returns ``(Path, None)`` on success and ``(None, err)`` on rejection.
    """
    try:
        cfg = config_manager.load()
        project_root = Path(getattr(cfg, "project_root", ".") or ".").resolve()
    except Exception:
        project_root = Path.cwd().resolve()

    candidate = Path(working_dir).expanduser()
    if not candidate.is_absolute():
        candidate = (project_root / candidate)
    try:
        resolved = candidate.resolve()
    except Exception as e:
        return None, f"Invalid working_dir {working_dir!r}: {e}"

    try:
        cfg_allow_outside = bool(config_manager.get("allow_outside_project", False))
    except Exception:
        cfg_allow_outside = False
    if os.environ.get("CODERAI_ALLOW_OUTSIDE_PROJECT") == "1" or cfg_allow_outside:
        return resolved, None

    try:
        resolved.relative_to(project_root)
    except ValueError:
        return None, (
            f"Refusing to run outside project root. working_dir={working_dir!r} "
            f"resolves to {resolved}, which is not under {project_root}. "
            "Set CODERAI_ALLOW_OUTSIDE_PROJECT=1 to override."
        )
    return resolved, None


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
    input: Optional[str] = Field(None, description="Optional text to send to the process stdin (max 64KB)")


class RunCommandTool(Tool):
    """Tool for executing shell commands with safety checks."""

    name = "run_command"
    description = "Execute a shell command and return its output. Dangerous commands require confirmation."
    parameters_model = RunCommandParams
    requires_confirmation = True
    timeout = None

    async def execute(
        self, command: str, working_dir: str = ".", timeout: int = 60,
        input: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute shell command with safety checks.

        Uses shlex.split + create_subprocess_exec for simple commands,
        falls back to create_subprocess_shell for complex shell syntax.
        """
        try:
            timeout = max(1, min(timeout, 3600))

            resolved_cwd, cwd_err = _resolve_working_dir(working_dir)
            if cwd_err:
                return {"success": False, "error": cwd_err, "error_code": "scope"}
            working_dir = str(resolved_cwd)

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
            needs_shell = _needs_shell(command)

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
                    return {
                        "success": False,
                        "error": (
                            f"Command has malformed quoting — cannot split safely: "
                            f"{command!r}. Check for unmatched quotes."
                        ),
                        "error_code": "malformed_command",
                    }

            # Wait for completion with timeout
            stdin_bytes = None
            if input is not None:
                stdin_bytes = input.encode("utf-8", errors="replace")[:65536]  # cap at 64KB
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(input=stdin_bytes), timeout=timeout
                )
                if process.returncode is None:
                    await process.wait()
            except asyncio.TimeoutError:
                try:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=2)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()
                except ProcessLookupError:
                    pass
                # Attempt to read any partial output that was buffered
                try:
                    stdout, stderr = await process.communicate()
                except Exception:
                    stdout, stderr = b"", b""
                stdout_str = stdout.decode("utf-8", errors="replace")
                stderr_str = stderr.decode("utf-8", errors="replace")
                return {
                    "success": False,
                    "error": f"Command timed out after {timeout} seconds",
                    "error_code": "timeout",
                    "stdout": stdout_str,
                    "stderr": stderr_str,
                    "returncode": process.returncode,
                }
            except asyncio.CancelledError:
                try:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), timeout=1)
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass
                raise

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
                stderr_str = (
                    stderr_str[:max_output // 2]
                    + f"\n\n... [truncated {len(stderr_str) - max_output} chars] ...\n\n"
                    + stderr_str[-max_output // 2:]
                )

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
    capture_output: bool = Field(False, description="Capture stdout/stderr for later retrieval via read_bg_output (default: false)")


class BgProcessInfo:
    """Tracks a background process plus its output buffer when capture is enabled."""

    def __init__(self, process: asyncio.subprocess.Process, command: str = ""):
        self.process = process
        self.command = command
        self.stdout_buf: List[str] = []
        self.stderr_buf: List[str] = []
        self._buf_bytes = 0
        self._max_buf_bytes = 65536  # 64KB total cap

    def _append(self, buf: List[str], data: str) -> None:
        if self._buf_bytes >= self._max_buf_bytes:
            return
        encoded = data.encode("utf-8", errors="replace")
        remaining = self._max_buf_bytes - self._buf_bytes
        chunk = encoded[:remaining]
        self._buf_bytes += len(chunk)
        buf.append(chunk.decode("utf-8", errors="replace"))


# Module-level registry of all tracked background processes.
_tracked_bg_processes: Dict[int, BgProcessInfo] = {}


class RunBackgroundTool(Tool):
    """Tool for starting background processes."""

    name = "run_background"
    description = "Start a command in the background (for long-running processes like servers)"
    parameters_model = RunBackgroundParams
    requires_confirmation = True

    def __init__(self):
        super().__init__()
        # Instance shares the module-level process registry
        self._processes = _tracked_bg_processes

    async def execute(self, command: str, working_dir: str = ".",
                      capture_output: bool = False) -> Dict[str, Any]:
        """Start background process with tracking.

        Uses the same exec-vs-shell heuristic as ``run_command`` — plain
        commands go through ``create_subprocess_exec`` (no shell interpretation),
        only commands with shell metacharacters fall back to the shell.
        """
        try:
            resolved_cwd, cwd_err = _resolve_working_dir(working_dir)
            if cwd_err:
                return {"success": False, "error": cwd_err, "error_code": "scope"}
            working_dir = str(resolved_cwd)

            if is_command_blocked(command):
                return {
                    "success": False,
                    "error": f"Command blocked for safety: {command}",
                    "blocked": True,
                }

            # Block interactive commands that would hang without a TTY
            from ..safeguards import is_interactive_command
            if is_interactive_command(command):
                logger.warning(f"Blocked interactive background command: {command}")
                return {
                    "success": False,
                    "error": (
                        "Command appears interactive (requires TTY/user input): "
                        f"{command!r}. Interactive commands cannot run in the background."
                    ),
                    "error_code": "interactive",
                    "interactive": True,
                }

            if is_command_dangerous(command):
                logger.warning(f"Executing potentially dangerous background command: {command}")

            command = _rewrite_command_aliases(command)
            needs_shell = _needs_shell(command)

            stdout_target = asyncio.subprocess.PIPE if capture_output else asyncio.subprocess.DEVNULL
            stderr_target = asyncio.subprocess.PIPE if capture_output else asyncio.subprocess.DEVNULL

            if needs_shell:
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=stdout_target,
                    stderr=stderr_target,
                    cwd=working_dir,
                )
            else:
                try:
                    args = shlex.split(command)
                    process = await asyncio.create_subprocess_exec(
                        *args,
                        stdout=stdout_target,
                        stderr=stderr_target,
                        cwd=working_dir,
                    )
                except ValueError:
                    return {
                        "success": False,
                        "error": (
                            f"Command has malformed quoting — cannot split safely: "
                            f"{command!r}. Check for unmatched quotes."
                        ),
                        "error_code": "malformed_command",
                    }

            # Track the process
            _ensure_atexit_cleanup()
            info = BgProcessInfo(process, command)
            self._processes[process.pid] = info

            if capture_output:
                # Start reader tasks to accumulate output
                async def _read_stream(stream, buf_list):
                    while True:
                        line_bytes = await stream.readline()
                        if not line_bytes:
                            break
                        info._append(buf_list, line_bytes.decode("utf-8", errors="replace"))
                if process.stdout:
                    asyncio.create_task(_read_stream(process.stdout, info.stdout_buf))
                if process.stderr:
                    asyncio.create_task(_read_stream(process.stderr, info.stderr_buf))

            return {
                "success": True,
                "pid": process.pid,
                "command": command,
                "capture_output": capture_output,
                "message": "Process started in background",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_tracked_processes(self) -> Dict[int, BgProcessInfo]:
        """Get all tracked background processes for this instance."""
        return self._processes

    def cleanup_finished(self) -> int:
        """Clean up finished processes from tracking. Returns count removed."""
        finished = [pid for pid, info in self._processes.items()
                    if info.process.returncode is not None]
        for pid in finished:
            del self._processes[pid]
        return len(finished)

    def terminate_all(self) -> int:
        """Forcefully terminate all remaining tracked processes."""
        terminated = 0
        for pid, info in dict(self._processes).items():
            if info.process.returncode is None:
                try:
                    info.process.kill()
                    terminated += 1
                except Exception:
                    pass
        self._processes.clear()
        return terminated


def _cleanup_all_background():
    """Terminate background processes from the shared module-level registry."""
    for pid, info in dict(_tracked_bg_processes).items():
        if info.process.returncode is None:
            try:
                info.process.kill()
            except Exception:
                pass
    _tracked_bg_processes.clear()


_atexit_registered = False


def _ensure_atexit_cleanup():
    """Register cleanup handler the first time a background process is started."""
    global _atexit_registered
    if not _atexit_registered:
        import atexit
        atexit.register(_cleanup_all_background)
        _atexit_registered = True


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
    category = "terminal"
    parameters_model = ListProcessesParams
    is_read_only = True

    async def execute(self) -> Dict[str, Any]:
        try:
            processes = []
            for pid, info in _tracked_bg_processes.items():
                processes.append(
                    {
                        "pid": pid,
                        "running": info.process.returncode is None,
                        "returncode": info.process.returncode,
                        "command": info.command,
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
    category = "terminal"
    parameters_model = KillProcessParams
    requires_confirmation = True

    async def execute(self, pid: int, force: bool = False) -> Dict[str, Any]:
        try:
            info = _tracked_bg_processes.get(pid)
            if info is None:
                return {
                    "success": False,
                    "error": (
                        f"No tracked background process with PID {pid}. "
                        "Only processes started by run_background can be terminated. "
                        "Use run_command with 'kill' if you need to signal an external process."
                    ),
                }

            if info.process.returncode is not None:
                return {
                    "success": False,
                    "error": f"Process {pid} has already exited (returncode={info.process.returncode}).",
                }

            sig = _signal.SIGKILL if force else _signal.SIGTERM
            info.process.send_signal(sig)
            try:
                await asyncio.wait_for(info.process.wait(), timeout=3)
            except asyncio.TimeoutError:
                info.process.kill()
                await info.process.wait()
            del _tracked_bg_processes[pid]
            return {
                "success": True,
                "pid": pid,
                "signal": "SIGKILL" if force else "SIGTERM",
                "message": f"Sent {'SIGKILL' if force else 'SIGTERM'} to process {pid}.",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Read background process output
# ---------------------------------------------------------------------------


class ReadBgOutputParams(BaseModel):
    pid: int = Field(..., description="Process ID (PID) of the background process")


class ReadBgOutputTool(Tool):
    """Read captured output from a background process started with capture_output=True."""

    name = "read_bg_output"
    description = "Read captured stdout/stderr from a background process started with capture_output=True"
    category = "terminal"
    parameters_model = ReadBgOutputParams
    is_read_only = True

    async def execute(self, pid: int) -> Dict[str, Any]:
        try:
            info = _tracked_bg_processes.get(pid)
            if info is None:
                return {
                    "success": False,
                    "error": (
                        f"No tracked background process with PID {pid}. "
                        "Only processes started by run_background with capture_output=True "
                        "can have their output read."
                    ),
                }

            stdout_text = "".join(info.stdout_buf)
            stderr_text = "".join(info.stderr_buf)
            running = info.process.returncode is None

            return {
                "success": True,
                "pid": pid,
                "running": running,
                "returncode": info.process.returncode,
                "stdout": stdout_text,
                "stderr": stderr_text,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
