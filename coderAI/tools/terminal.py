"""Terminal tools for command execution."""

import asyncio
import os
from typing import Any, Dict

from .base import Tool


class RunCommandTool(Tool):
    """Tool for executing shell commands."""

    name = "run_command"
    description = "Execute a shell command and return its output"

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
        """Execute shell command."""
        try:
            # Create subprocess
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

            return {
                "success": process.returncode == 0,
                "returncode": process.returncode,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
                "command": command,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


class RunBackgroundTool(Tool):
    """Tool for starting background processes."""

    name = "run_background"
    description = "Start a command in the background (for long-running processes)"

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
        """Start background process."""
        try:
            # Start process without waiting
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=working_dir,
            )

            return {
                "success": True,
                "pid": process.pid,
                "command": command,
                "message": "Process started in background",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

