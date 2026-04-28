"""Git tools for version control operations."""

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .base import Tool
from ..locks import resource_manager

logger = logging.getLogger(__name__)

MAX_GIT_OUTPUT_BYTES = 64_000

def _truncate_output(text: str, max_bytes: int = MAX_GIT_OUTPUT_BYTES) -> tuple[str, bool]:
    """Truncate text to max_bytes, returning (text, was_truncated)."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    head = encoded[:max_bytes].decode("utf-8", errors="replace")
    omitted = len(encoded) - max_bytes
    return head + f"\n\n[... truncated, {omitted} more bytes — re-run with a narrower scope ...]", True


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
    description = "Get the status of a git repository. Output truncated at 64KB."
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
            output, truncated = _truncate_output(output)
            # The first line from --porcelain -b is the branch header (## main...);
            # actual changes are on subsequent lines.
            lines = output.strip().split("\n")
            change_lines = [line for line in lines if line and not line.startswith("##")]
            return {
                "success": True,
                "status": output,
                "has_changes": bool(change_lines),
                "truncated": truncated,
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
    description = "View git diff for changes. Output truncated at 64KB; use file_path to narrow scope on huge diffs."
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
            diff, truncated = _truncate_output(diff)
            return {
                "success": True,
                "diff": diff,
                "has_diff": bool(diff.strip()),
                "truncated": truncated,
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
    description = "View git commit history. Output truncated at 64KB."
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
            log, truncated = _truncate_output(log)
            return {
                "success": True,
                "log": log,
                "commits": log.strip().split("\n") if log.strip() else [],
                "truncated": truncated,
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
    description = "List, create, or delete git branches. Output truncated at 64KB."
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
                    output, truncated = _truncate_output(stdout.decode("utf-8", errors="replace"))
                    branches = [
                        b.strip().lstrip("* ") for b in output.strip().split("\n") if b.strip()
                    ]
                    return {"success": True, "branches": branches, "count": len(branches), "truncated": truncated}

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



# ---------------------------------------------------------------------------
# Extended git tools: push, pull, merge, rebase, revert, reset,
#                     show, remote, blame, cherry-pick, tag
# ---------------------------------------------------------------------------


class GitPushParams(BaseModel):
    remote: str = Field("origin", description="Remote name (default: origin)")
    branch: Optional[str] = Field(None, description="Branch to push (default: current branch)")
    force: bool = Field(False, description="Force push using --force-with-lease for safety")
    set_upstream: bool = Field(False, description="Set upstream tracking branch (-u)")
    repo_path: str = Field(".", description="Path to the git repository")


class GitPushTool(Tool):
    """Push local commits to a remote repository."""

    name = "git_push"
    description = (
        "Push local commits to a remote repository. Uses --force-with-lease instead of "
        "--force to prevent overwriting upstream changes you haven't seen."
    )
    category = "git"
    parameters_model = GitPushParams
    requires_confirmation = True

    async def execute(
        self,
        remote: str = "origin",
        branch: Optional[str] = None,
        force: bool = False,
        set_upstream: bool = False,
        repo_path: str = ".",
    ) -> Dict[str, Any]:
        try:
            scope_error = await _validate_git_scope(repo_path)
            if scope_error:
                return scope_error

            cmd = ["git", "push"]
            if set_upstream:
                cmd.append("-u")
            if force:
                cmd.append("--force-with-lease")
            cmd.append(remote)
            if branch:
                cmd.append(branch)

            async with resource_manager.git_lock():
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


class GitPullParams(BaseModel):
    remote: str = Field("origin", description="Remote name (default: origin)")
    branch: Optional[str] = Field(None, description="Branch to pull (default: current tracking branch)")
    rebase: bool = Field(False, description="Pull with rebase instead of merge")
    repo_path: str = Field(".", description="Path to the git repository")


class GitPullTool(Tool):
    """Fetch and integrate changes from a remote repository."""

    name = "git_pull"
    description = "Fetch and merge (or rebase) changes from a remote repository into the current branch."
    category = "git"
    parameters_model = GitPullParams
    requires_confirmation = True

    async def execute(
        self,
        remote: str = "origin",
        branch: Optional[str] = None,
        rebase: bool = False,
        repo_path: str = ".",
    ) -> Dict[str, Any]:
        try:
            scope_error = await _validate_git_scope(repo_path)
            if scope_error:
                return scope_error

            cmd = ["git", "pull"]
            if rebase:
                cmd.append("--rebase")
            cmd.append(remote)
            if branch:
                cmd.append(branch)

            async with resource_manager.git_lock():
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


class GitMergeParams(BaseModel):
    branch: str = Field(..., description="Branch to merge into the current branch")
    no_ff: bool = Field(False, description="Force a merge commit even if fast-forward is possible")
    squash: bool = Field(False, description="Squash all commits into one before merging")
    message: Optional[str] = Field(None, description="Custom merge commit message")
    repo_path: str = Field(".", description="Path to the git repository")


class GitMergeTool(Tool):
    """Merge a branch into the current branch."""

    name = "git_merge"
    description = "Merge another branch into the current branch."
    category = "git"
    parameters_model = GitMergeParams
    requires_confirmation = True

    async def execute(
        self,
        branch: str,
        no_ff: bool = False,
        squash: bool = False,
        message: Optional[str] = None,
        repo_path: str = ".",
    ) -> Dict[str, Any]:
        try:
            scope_error = await _validate_git_scope(repo_path)
            if scope_error:
                return scope_error

            cmd = ["git", "merge"]
            if no_ff:
                cmd.append("--no-ff")
            if squash:
                cmd.append("--squash")
            if message:
                cmd.extend(["-m", message])
            cmd.append(branch)

            async with resource_manager.git_lock():
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


class GitRebaseParams(BaseModel):
    onto: str = Field(..., description="Branch or commit to rebase onto")
    abort: bool = Field(False, description="Abort an in-progress rebase")
    continue_rebase: bool = Field(False, description="Continue after resolving conflicts")
    repo_path: str = Field(".", description="Path to the git repository")


class GitRebaseTool(Tool):
    """Rebase the current branch onto another branch or commit."""

    name = "git_rebase"
    description = (
        "Rebase the current branch onto another branch. Also supports --abort and --continue "
        "to manage in-progress rebases after conflict resolution."
    )
    category = "git"
    parameters_model = GitRebaseParams
    requires_confirmation = True

    async def execute(
        self,
        onto: str,
        abort: bool = False,
        continue_rebase: bool = False,
        repo_path: str = ".",
    ) -> Dict[str, Any]:
        try:
            scope_error = await _validate_git_scope(repo_path)
            if scope_error:
                return scope_error

            if abort:
                cmd = ["git", "rebase", "--abort"]
            elif continue_rebase:
                cmd = ["git", "rebase", "--continue"]
            else:
                cmd = ["git", "rebase", onto]

            async with resource_manager.git_lock():
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


class GitRevertParams(BaseModel):
    commit: str = Field(..., description="Commit hash to revert")
    no_commit: bool = Field(False, description="Stage the revert without creating a commit")
    repo_path: str = Field(".", description="Path to the git repository")


class GitRevertTool(Tool):
    """Create a new commit that undoes changes from a previous commit."""

    name = "git_revert"
    description = (
        "Create a new commit that reverses the changes introduced by an existing commit. "
        "Safe for shared history — does not rewrite commits."
    )
    category = "git"
    parameters_model = GitRevertParams
    requires_confirmation = True

    async def execute(
        self,
        commit: str,
        no_commit: bool = False,
        repo_path: str = ".",
    ) -> Dict[str, Any]:
        try:
            scope_error = await _validate_git_scope(repo_path)
            if scope_error:
                return scope_error

            cmd = ["git", "revert", "--no-edit"]
            if no_commit:
                cmd.append("-n")
            cmd.append(commit)

            async with resource_manager.git_lock():
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


class GitResetParams(BaseModel):
    ref: str = Field("HEAD", description="Commit reference to reset to (default: HEAD)")
    mode: str = Field(
        "mixed",
        description=(
            "Reset mode: 'soft' (keep staged + working tree), "
            "'mixed' (unstage but keep working tree), "
            "'hard' (discard all changes — destructive)."
        ),
    )
    repo_path: str = Field(".", description="Path to the git repository")


class GitResetTool(Tool):
    """Reset the current branch HEAD to a specified state."""

    name = "git_reset"
    description = (
        "Reset the current HEAD to a specified commit. 'soft' keeps staged changes, "
        "'mixed' unstages them, 'hard' discards all local changes (destructive)."
    )
    category = "git"
    parameters_model = GitResetParams
    requires_confirmation = True

    async def execute(
        self,
        ref: str = "HEAD",
        mode: str = "mixed",
        repo_path: str = ".",
    ) -> Dict[str, Any]:
        try:
            scope_error = await _validate_git_scope(repo_path)
            if scope_error:
                return scope_error

            if mode not in ("soft", "mixed", "hard"):
                return {"success": False, "error": f"Invalid mode '{mode}'. Use soft, mixed, or hard."}

            cmd = ["git", "reset", f"--{mode}", ref]

            async with resource_manager.git_lock():
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


class GitShowParams(BaseModel):
    ref: str = Field("HEAD", description="Commit hash, tag, or branch to inspect (default: HEAD)")
    stat_only: bool = Field(False, description="Show only file change stats, not the full diff")
    repo_path: str = Field(".", description="Path to the git repository")


class GitShowTool(Tool):
    """Display details and diff of a specific commit."""

    name = "git_show"
    description = "Show the commit message, author, date, and diff for a specific commit or reference. Output truncated at 64KB."
    category = "git"
    parameters_model = GitShowParams
    is_read_only = True

    async def execute(
        self,
        ref: str = "HEAD",
        stat_only: bool = False,
        repo_path: str = ".",
    ) -> Dict[str, Any]:
        try:
            cmd = ["git", "show"]
            if stat_only:
                cmd.append("--stat")
            cmd.append(ref)

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=repo_path,
            )
            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                return {"success": False, "error": stderr.decode("utf-8", errors="replace").strip()}

            output, truncated = _truncate_output(stdout.decode("utf-8", errors="replace"))
            return {"success": True, "output": output, "truncated": truncated}
        except Exception as e:
            return {"success": False, "error": str(e)}


class GitRemoteParams(BaseModel):
    action: str = Field(
        ...,
        description=(
            "Action: 'list' (show remotes), 'add' (add a remote), "
            "'remove' (delete a remote), 'set-url' (change URL of a remote)."
        ),
    )
    name: Optional[str] = Field(None, description="Remote name (required for add/remove/set-url)")
    url: Optional[str] = Field(None, description="Remote URL (required for add/set-url)")
    repo_path: str = Field(".", description="Path to the git repository")


class GitRemoteTool(Tool):
    """Manage git remote connections."""

    name = "git_remote"
    description = "List, add, remove, or update git remote repository connections."
    category = "git"
    parameters_model = GitRemoteParams
    requires_confirmation = True

    async def execute(
        self,
        action: str,
        name: Optional[str] = None,
        url: Optional[str] = None,
        repo_path: str = ".",
    ) -> Dict[str, Any]:
        try:
            async with resource_manager.git_lock():
                if action == "list":
                    cmd = ["git", "remote", "-v"]
                elif action == "add":
                    if not name or not url:
                        return {"success": False, "error": "'name' and 'url' required for 'add'."}
                    cmd = ["git", "remote", "add", name, url]
                elif action == "remove":
                    if not name:
                        return {"success": False, "error": "'name' required for 'remove'."}
                    cmd = ["git", "remote", "remove", name]
                elif action == "set-url":
                    if not name or not url:
                        return {"success": False, "error": "'name' and 'url' required for 'set-url'."}
                    cmd = ["git", "remote", "set-url", name, url]
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


class GitBlameParams(BaseModel):
    file_path: str = Field(..., description="File path to annotate")
    start_line: Optional[int] = Field(None, description="First line of the range to blame")
    end_line: Optional[int] = Field(None, description="Last line of the range to blame")
    repo_path: str = Field(".", description="Path to the git repository")


class GitBlameTool(Tool):
    """Show which commit last modified each line of a file."""

    name = "git_blame"
    description = "Annotate each line of a file with the commit and author that last changed it. Output truncated at 64KB."
    category = "git"
    parameters_model = GitBlameParams
    is_read_only = True

    async def execute(
        self,
        file_path: str,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        repo_path: str = ".",
    ) -> Dict[str, Any]:
        try:
            cmd = ["git", "blame", "--porcelain"]
            if start_line and end_line:
                cmd.extend([f"-L{start_line},{end_line}"])
            elif start_line:
                cmd.extend([f"-L{start_line},+30"])
            cmd.append(file_path)

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=repo_path,
            )
            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                return {"success": False, "error": stderr.decode("utf-8", errors="replace").strip()}

            raw = stdout.decode("utf-8", errors="replace")
            raw, truncated = _truncate_output(raw)
            lines: List[Dict[str, str]] = []
            current: Dict[str, str] = {}
            for line in raw.splitlines():
                if line.startswith("\t"):
                    current["code"] = line[1:]
                    lines.append(current)
                    current = {}
                elif " " in line:
                    parts = line.split(" ", 3)
                    if len(parts[0]) == 40:
                        current["commit"] = parts[0]
                        if len(parts) >= 3:
                            current["line"] = parts[2]
                    else:
                        key, _, value = line.partition(" ")
                        current[key] = value

            return {"success": True, "file": file_path, "annotations": lines, "count": len(lines), "truncated": truncated}
        except Exception as e:
            return {"success": False, "error": str(e)}


class GitCherryPickParams(BaseModel):
    commits: List[str] = Field(..., description="List of commit hashes to cherry-pick")
    no_commit: bool = Field(False, description="Apply changes without committing")
    repo_path: str = Field(".", description="Path to the git repository")


class GitCherryPickTool(Tool):
    """Apply specific commits from another branch onto the current branch."""

    name = "git_cherry_pick"
    description = "Apply one or more specific commits from another branch onto the current branch."
    category = "git"
    parameters_model = GitCherryPickParams
    requires_confirmation = True

    async def execute(
        self,
        commits: List[str],
        no_commit: bool = False,
        repo_path: str = ".",
    ) -> Dict[str, Any]:
        try:
            scope_error = await _validate_git_scope(repo_path)
            if scope_error:
                return scope_error

            cmd = ["git", "cherry-pick"]
            if no_commit:
                cmd.append("-n")
            cmd.extend(commits)

            async with resource_manager.git_lock():
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


class GitTagParams(BaseModel):
    action: str = Field(
        ...,
        description="Action: 'list' (show tags), 'create' (add tag), 'delete' (remove tag).",
    )
    tag_name: Optional[str] = Field(None, description="Tag name (required for create/delete)")
    message: Optional[str] = Field(None, description="Annotated tag message (omit for lightweight tag)")
    ref: str = Field("HEAD", description="Commit to tag (default: HEAD, only for create)")
    repo_path: str = Field(".", description="Path to the git repository")


class GitTagTool(Tool):
    """List, create, or delete git tags."""

    name = "git_tag"
    description = "Manage git tags: list existing tags, create lightweight or annotated tags, or delete tags."
    category = "git"
    parameters_model = GitTagParams
    requires_confirmation = True

    async def execute(
        self,
        action: str,
        tag_name: Optional[str] = None,
        message: Optional[str] = None,
        ref: str = "HEAD",
        repo_path: str = ".",
    ) -> Dict[str, Any]:
        try:
            async with resource_manager.git_lock():
                if action == "list":
                    cmd = ["git", "tag", "--list", "--sort=-version:refname"]
                elif action == "create":
                    if not tag_name:
                        return {"success": False, "error": "'tag_name' required for create."}
                    if message:
                        cmd = ["git", "tag", "-a", tag_name, "-m", message, ref]
                    else:
                        cmd = ["git", "tag", tag_name, ref]
                elif action == "delete":
                    if not tag_name:
                        return {"success": False, "error": "'tag_name' required for delete."}
                    cmd = ["git", "tag", "-d", tag_name]
                else:
                    return {"success": False, "error": f"Unknown action: {action}"}

                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=repo_path,
                )
                stdout, stderr = await process.communicate()

            output = stdout.decode("utf-8", errors="replace")
            err = stderr.decode("utf-8", errors="replace")
            if action == "list":
                tags = [t for t in output.strip().splitlines() if t]
                return {"success": True, "tags": tags, "count": len(tags)}
            return {
                "success": process.returncode == 0,
                "output": (output + err).strip(),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Git fetch
# ---------------------------------------------------------------------------


class GitFetchParams(BaseModel):
    remote: str = Field("origin", description="Remote name (default: origin)")
    branch: Optional[str] = Field(None, description="Specific branch to fetch (default: all)")
    prune: bool = Field(False, description="Remove remote-tracking branches that no longer exist on the remote (-p)")
    repo_path: str = Field(".", description="Path to the git repository")


class GitFetchTool(Tool):
    """Download objects and refs from a remote repository without merging."""

    name = "git_fetch"
    description = "Download objects and refs from a remote repository. Unlike git_pull, this does not merge changes."
    category = "git"
    parameters_model = GitFetchParams
    is_read_only = True

    async def execute(
        self,
        remote: str = "origin",
        branch: Optional[str] = None,
        prune: bool = False,
        repo_path: str = ".",
    ) -> Dict[str, Any]:
        try:
            scope_error = await _validate_git_scope(repo_path)
            if scope_error:
                return scope_error

            cmd = ["git", "fetch", remote]
            if prune:
                cmd.append("--prune")
            if branch:
                cmd.append(branch)

            async with resource_manager.git_lock():
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
