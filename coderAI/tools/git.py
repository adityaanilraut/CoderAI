"""Git tools for version control operations."""

import asyncio
from typing import Any, Dict

from .base import Tool


class GitStatusTool(Tool):
    """Tool for checking git repository status."""

    name = "git_status"
    description = "Get the status of a git repository"

    def get_parameters(self) -> Dict[str, Any]:
        """Get parameters schema."""
        return {
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Path to the git repository (default: current directory)",
                },
            },
            "required": [],
        }

    async def execute(self, repo_path: str = ".") -> Dict[str, Any]:
        """Get git status."""
        try:
            process = await asyncio.create_subprocess_shell(
                "git status --porcelain -b",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=repo_path,
            )
            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                return {
                    "success": False,
                    "error": stderr.decode("utf-8", errors="replace"),
                }

            output = stdout.decode("utf-8", errors="replace")
            return {
                "success": True,
                "status": output,
                "has_changes": bool(output.strip()),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


class GitDiffTool(Tool):
    """Tool for viewing git diffs."""

    name = "git_diff"
    description = "View git diff for changes"

    def get_parameters(self) -> Dict[str, Any]:
        """Get parameters schema."""
        return {
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Path to the git repository (default: current directory)",
                },
                "file_path": {
                    "type": "string",
                    "description": "Optional specific file to diff",
                },
                "staged": {
                    "type": "boolean",
                    "description": "Show staged changes only (default: false)",
                },
            },
            "required": [],
        }

    async def execute(
        self, repo_path: str = ".", file_path: str = None, staged: bool = False
    ) -> Dict[str, Any]:
        """Get git diff."""
        try:
            cmd = "git diff"
            if staged:
                cmd += " --cached"
            if file_path:
                cmd += f" {file_path}"

            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=repo_path,
            )
            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                return {
                    "success": False,
                    "error": stderr.decode("utf-8", errors="replace"),
                }

            diff = stdout.decode("utf-8", errors="replace")
            return {
                "success": True,
                "diff": diff,
                "has_diff": bool(diff.strip()),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


class GitCommitTool(Tool):
    """Tool for creating git commits."""

    name = "git_commit"
    description = "Create a git commit with staged changes"

    def get_parameters(self) -> Dict[str, Any]:
        """Get parameters schema."""
        return {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Commit message",
                },
                "repo_path": {
                    "type": "string",
                    "description": "Path to the git repository (default: current directory)",
                },
            },
            "required": ["message"],
        }

    async def execute(self, message: str, repo_path: str = ".") -> Dict[str, Any]:
        """Create git commit."""
        try:
            # Escape message for shell
            escaped_message = message.replace('"', '\\"')
            cmd = f'git commit -m "{escaped_message}"'

            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=repo_path,
            )
            stdout, stderr = await process.communicate()

            output = stdout.decode("utf-8", errors="replace")
            error = stderr.decode("utf-8", errors="replace")

            return {
                "success": process.returncode == 0,
                "output": output + error,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


class GitLogTool(Tool):
    """Tool for viewing git history."""

    name = "git_log"
    description = "View git commit history"

    def get_parameters(self) -> Dict[str, Any]:
        """Get parameters schema."""
        return {
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Path to the git repository (default: current directory)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of commits to show (default: 10)",
                },
            },
            "required": [],
        }

    async def execute(self, repo_path: str = ".", limit: int = 10) -> Dict[str, Any]:
        """Get git log."""
        try:
            cmd = f"git log --oneline -n {limit}"

            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=repo_path,
            )
            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                return {
                    "success": False,
                    "error": stderr.decode("utf-8", errors="replace"),
                }

            log = stdout.decode("utf-8", errors="replace")
            return {
                "success": True,
                "log": log,
                "commits": log.strip().split("\n") if log.strip() else [],
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

