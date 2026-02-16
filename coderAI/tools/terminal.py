"""Terminal tools for command execution."""

import asyncio
import logging
import shlex
from typing import Any, Dict

from .base import Tool

logger = logging.getLogger(__name__)

# Commands that are blocked by default for safety
BLOCKED_PATTERNS = [
    "rm -rf /",
    "rm -rf ~",
    "rm -rf /*",
    "mkfs",
    "dd if=",
    ":(){:|:&};:",
    "> /dev/sda",
    "chmod -R 777 /",
    "shutdown",
    "reboot",
    "halt",
    "format c:",
    "/dev/null",
    "wget|sh",
    "curl|sh",
    "curl|bash",
    "wget|bash",
]

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


def is_command_blocked(command: str) -> bool:
    """Check if a command is in the blocklist."""
    cmd_lower = command.strip().lower()
    return any(blocked in cmd_lower for blocked in BLOCKED_PATTERNS)


def is_command_dangerous(command: str) -> bool:
    """Check if a command should require confirmation."""
    cmd_lower = command.strip().lower()
    return any(cmd_lower.startswith(prefix) for prefix in DANGEROUS_PREFIXES)


class RunCommandTool(Tool):
    """Tool for executing shell commands with safety checks."""

    name = "run_command"
    description = "Execute a shell command and return its output. Dangerous commands require confirmation."

    def get_parameters(self) -> Dict[str, Any]:
        """Get parameters schema."""
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute",
                },
                "working_dir": {
                    "type": "string",
                    "description": "Working directory for the command (default: current)",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 60)",
                },
            },
            "required": ["command"],
        }

    async def execute(
        self, command: str, working_dir: str = ".", timeout: int = 60
    ) -> Dict[str, Any]:
        """Execute shell command with safety checks.

        Uses shlex.split + create_subprocess_exec for simple commands,
        falls back to create_subprocess_shell for complex shell syntax.
        """
        try:
            # Block known destructive commands
            if is_command_blocked(command):
                return {
                    "success": False,
                    "error": f"Command blocked for safety: {command}",
                    "blocked": True,
                }

            # Flag dangerous commands (logged as warning)
            if is_command_dangerous(command):
                logger.warning(f"Executing potentially dangerous command: {command}")
                return {
                    "success": False,
                    "error": (
                        f"Command '{command}' is flagged as potentially dangerous. "
                        "This command involves file deletion, system changes, or "
                        "package management. Please confirm with the user first."
                    ),
                    "dangerous": True,
                    "hint": "Ask the user to confirm they want to run this command.",
                }

            # Try to use exec (no shell) for simple commands
            needs_shell = any(c in command for c in ['|', '>', '<', '&&', '||', ';', '`', '$', '*', '?', '~'])

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
            except asyncio.TimeoutError:
                process.kill()
                return {
                    "success": False,
                    "error": f"Command timed out after {timeout} seconds",
                }

            # Truncate very large output to prevent context overflow
            stdout_str = stdout.decode("utf-8", errors="replace")
            stderr_str = stderr.decode("utf-8", errors="replace")

            max_output = 10000
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


class RunBackgroundTool(Tool):
    """Tool for starting background processes."""

    name = "run_background"
    description = "Start a command in the background (for long-running processes like servers)"

    # Track background processes so they can be checked/killed later
    _processes: Dict[int, asyncio.subprocess.Process] = {}

    def get_parameters(self) -> Dict[str, Any]:
        """Get parameters schema."""
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute in background",
                },
                "working_dir": {
                    "type": "string",
                    "description": "Working directory for the command (default: current)",
                },
            },
            "required": ["command"],
        }

    async def execute(self, command: str, working_dir: str = ".") -> Dict[str, Any]:
        """Start background process with tracking."""
        try:
            if is_command_blocked(command):
                return {
                    "success": False,
                    "error": f"Command blocked for safety: {command}",
                    "blocked": True,
                }

            if is_command_dangerous(command):
                return {
                    "success": False,
                    "error": (
                        f"Command '{command}' is flagged as potentially dangerous. "
                        "Please confirm with the user first."
                    ),
                    "dangerous": True,
                }

            # Start process, redirect to DEVNULL to prevent pipe deadlocks
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

    @classmethod
    def get_tracked_processes(cls) -> Dict[int, asyncio.subprocess.Process]:
        """Get all tracked background processes."""
        return cls._processes

    @classmethod
    def cleanup_finished(cls) -> int:
        """Clean up finished processes from tracking. Returns count removed."""
        finished = [pid for pid, proc in cls._processes.items() if proc.returncode is not None]
        for pid in finished:
            del cls._processes[pid]
        return len(finished)
