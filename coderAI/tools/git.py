"""Git tools for version control operations."""

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .base import Tool
from ..locks import resource_manager

logger = logging.getLogger(__name__)


async def _validate_git_scope(repo_path: str) -> Optional[Dict[str, Any]]:
    """Validate that the git root matches the intended repo_path.

    Returns an error dict if scope is mismatched, or None if valid.
    Mutating git operations should call this before proceeding.
    """
    from ..safeguards import resolve_git_root
    result = await resolve_git_root(repo_path)

    if result["git_root"] is None:
        return {
            "success": False,
            "error": f"Not a git repository: {repo_path}",
            "error_code": "not_git_repo",
        }

    if not result["matches_expected"]:
        return {
            "success": False,
            "error": (
                f"Git scope mismatch: intended repo_path={str(Path(repo_path).resolve())} "
                f"but git root={result['git_root']}. "
                "Refusing to operate to prevent affecting files outside the intended project."
            ),
            "error_code": "scope_mismatch",
            "git_root": result["git_root"],
        }

    logger.debug(f"Git scope validated: root={result['git_root']}")
    return None


class GitAddParams(BaseModel):
    files: List[str] = Field(..., description="List of explicit file paths to stage. Do NOT use ['.'] — specify individual files.")
    repo_path: str = Field(".", description="Path to the git repository (default: current directory)")


class GitAddTool(Tool):
    """Tool for staging files in git."""

    name = "git_add"
    description = "Stage specific files for the next git commit. You MUST list individual file paths — 'git add .' is not allowed."
    parameters_model = GitAddParams
    requires_confirmation = True

    async def execute(self, files: list, repo_path: str = ".") -> Dict[str, Any]:
        """Stage files for git commit with safety checks."""
        try:
            # Reject 'git add .'
            if files == ["."] or files == ["*"]:
                return {
                    "success": False,
                    "error": (
                        "Refusing to stage all files ('git add .'). "
                        "Please specify explicit file paths to stage only "
                        "the files you intentionally created or modified."
                    ),
                    "error_code": "unsafe_staging",
                }

            # Validate git scope
            scope_error = await _validate_git_scope(repo_path)
            if scope_error:
                return scope_error

            # Filter out junk files
            from ..safeguards import filter_stageable_files
            allowed, rejected = filter_stageable_files(files, repo_path)

            if rejected:
                logger.info(
                    f"git_add: filtered {len(rejected)} junk file(s): {rejected}"
                )

            if not allowed:
                return {
                    "success": False,
                    "error": (
                        f"All requested files were filtered as junk/internal artifacts: "
                        f"{rejected}. No files staged."
                    ),
                    "error_code": "all_filtered",
                }

            async with resource_manager.git_lock():
                cmd = ["git", "add"] + allowed
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

            result = {
                "success": True,
                "files_staged": allowed,
                "message": f"Staged {len(allowed)} file(s)",
            }
            if rejected:
                result["files_filtered"] = rejected
                result["filter_note"] = (
                    f"{len(rejected)} file(s) auto-filtered (junk/internal artifacts)"
                )
            logger.info(f"git_add: staged {allowed}")
            return result
        except Exception as e:
            return {"success": False, "error": str(e)}


class GitStatusParams(BaseModel):
    repo_path: str = Field(".", description="Path to the git repository (default: current directory)")


class GitStatusTool(Tool):
    """Tool for checking git repository status."""

    name = "git_status"
    description = "Get the status of a git repository"
    parameters_model = GitStatusParams
    is_read_only = True

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
            # The first line from --porcelain -b is the branch header (## main...);
            # actual changes are on subsequent lines.
            lines = output.strip().split("\n")
            change_lines = [line for line in lines if line and not line.startswith("##")]
            return {
                "success": True,
                "status": output,
                "has_changes": bool(change_lines),
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
    is_read_only = True

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
    requires_confirmation = True

    async def execute(self, message: str, repo_path: str = ".") -> Dict[str, Any]:
        """Create git commit with scope validation."""
        try:
            # Validate git scope before committing
            scope_error = await _validate_git_scope(repo_path)
            if scope_error:
                return scope_error

            async with resource_manager.git_lock():
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
            logger.info(f"git_commit: repo={repo_path} message={message!r}")

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
    is_read_only = True

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


# --- Extended Git Tools ---


class GitBranchParams(BaseModel):
    action: str = Field(
        ...,
        description=(
            "Action: 'list' (show branches), 'create' (new branch), "
            "'delete' (remove branch)."
        ),
    )
    branch_name: Optional[str] = Field(
        None, description="Branch name (required for 'create' and 'delete')."
    )
    repo_path: str = Field(".", description="Path to the git repository (default: current directory)")


class GitBranchTool(Tool):
    """Tool for managing git branches."""

    name = "git_branch"
    description = "List, create, or delete git branches"
    parameters_model = GitBranchParams
    requires_confirmation = True

    async def execute(
        self, action: str, branch_name: Optional[str] = None, repo_path: str = "."
    ) -> Dict[str, Any]:
        try:
            async with resource_manager.git_lock():
                if action == "list":
                    process = await asyncio.create_subprocess_exec(
                        "git", "branch", "-a",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=repo_path,
                    )
                    stdout, stderr = await process.communicate()
                    if process.returncode != 0:
                        return {"success": False, "error": stderr.decode("utf-8", errors="replace")}
                    branches = [
                        b.strip().lstrip("* ") for b in stdout.decode("utf-8").strip().split("\n") if b.strip()
                    ]
                    return {"success": True, "branches": branches, "count": len(branches)}

                elif action == "create":
                    if not branch_name:
                        return {"success": False, "error": "branch_name is required for 'create'."}
                    process = await asyncio.create_subprocess_exec(
                        "git", "branch", branch_name,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=repo_path,
                    )
                    stdout, stderr = await process.communicate()
                    if process.returncode != 0:
                        return {"success": False, "error": stderr.decode("utf-8", errors="replace")}
                    return {"success": True, "message": f"Branch '{branch_name}' created."}

                elif action == "delete":
                    if not branch_name:
                        return {"success": False, "error": "branch_name is required for 'delete'."}
                    process = await asyncio.create_subprocess_exec(
                        "git", "branch", "-d", branch_name,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=repo_path,
                    )
                    stdout, stderr = await process.communicate()
                    output = stdout.decode("utf-8", errors="replace") + stderr.decode("utf-8", errors="replace")
                    return {"success": process.returncode == 0, "output": output}

                else:
                    return {"success": False, "error": f"Unknown action: {action}"}

        except Exception as e:
            return {"success": False, "error": str(e)}


class GitCheckoutParams(BaseModel):
    branch: str = Field(..., description="Branch name or commit hash to checkout")
    create: bool = Field(False, description="Create the branch if it doesn't exist (-b flag)")
    repo_path: str = Field(".", description="Path to the git repository (default: current directory)")


class GitCheckoutTool(Tool):
    """Tool for switching git branches."""

    name = "git_checkout"
    description = "Switch to a different git branch or create and switch to a new branch"
    parameters_model = GitCheckoutParams
    requires_confirmation = True

    async def execute(
        self, branch: str, create: bool = False, repo_path: str = "."
    ) -> Dict[str, Any]:
        try:
            # Validate git scope before checkout
            scope_error = await _validate_git_scope(repo_path)
            if scope_error:
                return scope_error

            async with resource_manager.git_lock():
                # Log branch before switching
                from ..safeguards import get_current_branch
                branch_before = await get_current_branch(repo_path)
                logger.info(
                    f"git_checkout: branch_before={branch_before} "
                    f"target={branch} create={create} repo={repo_path}"
                )

                cmd = ["git", "checkout"]
                if create:
                    cmd.append("-b")
                cmd.append(branch)

                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=repo_path,
                )
                stdout, stderr = await process.communicate()
                output = stdout.decode("utf-8", errors="replace") + stderr.decode("utf-8", errors="replace")

            return {"success": process.returncode == 0, "output": output.strip()}

        except Exception as e:
            return {"success": False, "error": str(e)}


class GitStashParams(BaseModel):
    action: str = Field(
        ...,
        description="Action: 'push' (stash changes), 'pop' (apply and remove top stash), 'list' (show stashes), 'drop' (remove a stash entry).",
    )
    message: Optional[str] = Field(None, description="Optional message for 'push' action.")
    stash_index: int = Field(0, description="Stash index for 'pop' or 'drop' (default: 0 = latest).")
    repo_path: str = Field(".", description="Path to the git repository (default: current directory)")


class GitStashTool(Tool):
    """Tool for git stash operations."""

    name = "git_stash"
    description = "Stash or restore uncommitted changes (push, pop, list, drop)"
    parameters_model = GitStashParams
    requires_confirmation = True

    async def execute(
        self,
        action: str,
        message: Optional[str] = None,
        stash_index: int = 0,
        repo_path: str = ".",
    ) -> Dict[str, Any]:
        try:
            async with resource_manager.git_lock():
                if action == "push":
                    cmd = ["git", "stash", "push"]
                    if message:
                        cmd.extend(["-m", message])
                elif action == "pop":
                    cmd = ["git", "stash", "pop", f"stash@{{{stash_index}}}"]
                elif action == "list":
                    cmd = ["git", "stash", "list"]
                elif action == "drop":
                    cmd = ["git", "stash", "drop", f"stash@{{{stash_index}}}"]
                else:
                    return {"success": False, "error": f"Unknown action: {action}"}

                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=repo_path,
                )
                stdout, stderr = await process.communicate()
                output = stdout.decode("utf-8", errors="replace") + stderr.decode("utf-8", errors="replace")

            return {"success": process.returncode == 0, "output": output.strip()}

        except Exception as e:
            return {"success": False, "error": str(e)}

