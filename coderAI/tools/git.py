"""Git tools for version control operations."""

import asyncio
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .base import Tool


class GitAddParams(BaseModel):
    files: List[str] = Field(..., description="List of file paths to stage (use ['.'] to stage all changes)")
    repo_path: str = Field(".", description="Path to the git repository (default: current directory)")


class GitAddTool(Tool):
    """Tool for staging files in git."""

    name = "git_add"
    description = "Stage files for the next git commit"
    parameters_model = GitAddParams

    async def execute(self, files: list, repo_path: str = ".") -> Dict[str, Any]:
        """Stage files for git commit."""
        try:
            cmd = ["git", "add"] + files
            process = await asyncio.create_subprocess_exec(
                *cmd,
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

            return {
                "success": True,
                "files": files,
                "message": f"Staged {len(files)} path(s)",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


class GitStatusParams(BaseModel):
    repo_path: str = Field(".", description="Path to the git repository (default: current directory)")


class GitStatusTool(Tool):
    """Tool for checking git repository status."""

    name = "git_status"
    description = "Get the status of a git repository"
    parameters_model = GitStatusParams

    async def execute(self, repo_path: str = ".") -> Dict[str, Any]:
        """Get git status."""
        try:
            process = await asyncio.create_subprocess_exec(
                "git", "status", "--porcelain", "-b",
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


class GitDiffParams(BaseModel):
    repo_path: str = Field(".", description="Path to the git repository (default: current directory)")
    file_path: Optional[str] = Field(None, description="Optional specific file to diff")
    staged: bool = Field(False, description="Show staged changes only (default: false)")


class GitDiffTool(Tool):
    """Tool for viewing git diffs."""

    name = "git_diff"
    description = "View git diff for changes"
    parameters_model = GitDiffParams

    async def execute(
        self, repo_path: str = ".", file_path: str = None, staged: bool = False
    ) -> Dict[str, Any]:
        """Get git diff."""
        try:
            cmd = ["git", "diff"]
            if staged:
                cmd.append("--cached")
            if file_path:
                cmd.append(file_path)

            process = await asyncio.create_subprocess_exec(
                *cmd,
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


class GitCommitParams(BaseModel):
    message: str = Field(..., description="Commit message")
    repo_path: str = Field(".", description="Path to the git repository (default: current directory)")


class GitCommitTool(Tool):
    """Tool for creating git commits."""

    name = "git_commit"
    description = "Create a git commit with staged changes"
    parameters_model = GitCommitParams

    async def execute(self, message: str, repo_path: str = ".") -> Dict[str, Any]:
        """Create git commit."""
        try:
            # Use create_subprocess_exec to avoid shell injection
            process = await asyncio.create_subprocess_exec(
                "git", "commit", "-m", message,
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


class GitLogParams(BaseModel):
    repo_path: str = Field(".", description="Path to the git repository (default: current directory)")
    limit: int = Field(10, description="Number of commits to show (default: 10)")


class GitLogTool(Tool):
    """Tool for viewing git history."""

    name = "git_log"
    description = "View git commit history"
    parameters_model = GitLogParams

    async def execute(self, repo_path: str = ".", limit: int = 10) -> Dict[str, Any]:
        """Get git log."""
        try:
            process = await asyncio.create_subprocess_exec(
                "git", "log", "--oneline", "-n", str(limit),
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
