"""Git tools for version control operations."""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from coderAI.tools.base import Tool
from coderAI.core.tool_error_codes import ToolErrorCode
from coderAI.system.locks import resource_manager
from coderAI.system.proc import run_scrubbed
from coderAI.system.safeguards import truncate_output

logger = logging.getLogger(__name__)

MAX_GIT_OUTPUT_CHARS = 64_000

_GIT_TRUNCATION_MARKER = "... [truncated {omitted} chars — re-run with a narrower scope] ..."


def _truncate_output(text: str, max_chars: int = MAX_GIT_OUTPUT_CHARS) -> tuple[str, bool]:
    """Truncate git output via the shared helper, returning (text, was_truncated)."""
    return truncate_output(
        text, max_chars=max_chars, mode="head_tail", marker=_GIT_TRUNCATION_MARKER
    )


def _reject_option_like(value: Optional[str], label: str) -> Optional[Dict[str, Any]]:
    """Guard against argument injection via a user-controlled positional.

    A value that begins with ``-`` (e.g. ``--output=/etc/passwd``) would be
    parsed by git as an *option* rather than a ref/path, letting a caller
    create or truncate arbitrary files through read-only tools like
    ``git diff``/``git show``/``git blame``. Refs and paths never legitimately
    start with ``-``, so reject it. Returns an error dict, or ``None`` if OK.
    """
    if value is not None and value.startswith("-"):
        return {
            "success": False,
            "error": (
                f"Refusing {label} that starts with '-': {value!r}. It could be "
                "interpreted as a git option (e.g. --output=…). Provide a plain value."
            ),
            "error_code": ToolErrorCode.SCOPE,
        }
    return None


async def _run_git_command(
    args: List[str],
    repo_path: str,
    *,
    needs_lock: bool = False,
    validate_scope: bool = True,
) -> Dict[str, Any]:
    """Run a git command with scope validation and optional lock acquisition.

    Returns ``{"success": False, ...}`` on scope validation error, or
    ``{"success": True, "returncode": int, "stdout": bytes, "stderr": bytes}``
    on command completion.  The caller is responsible for checking returncode
    and formatting the tool-level result.
    """
    if validate_scope:
        scope_error = await _validate_git_scope(repo_path)
        if scope_error:
            return scope_error

    async def _exec():
        # Scrub secrets from the child env: git subcommands can shell out
        # (hooks, credential helpers, ``git config alias.* = !sh -c …``), so an
        # inherited ``$ANTHROPIC_API_KEY`` etc. must not be reachable. scrub_env
        # is a denylist, so HOME/PATH/GIT_* the user set survive untouched.
        returncode, stdout, stderr, _ = await run_scrubbed(
            ["git", *args], cwd=repo_path, shell=False
        )
        return returncode, stdout, stderr

    if needs_lock:
        async with resource_manager.git_lock():
            returncode, stdout, stderr = await _exec()
    else:
        returncode, stdout, stderr = await _exec()

    return {
        "success": True,
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
    }


async def _validate_git_scope(repo_path: str) -> Optional[Dict[str, Any]]:
    """Validate that the git root matches the intended repo_path.

    Returns an error dict if scope is mismatched, or None if valid.
    Mutating git operations should call this before proceeding.
    """
    from coderAI.system.safeguards import resolve_git_root

    result = await resolve_git_root(repo_path)

    if result["git_root"] is None:
        return {
            "success": False,
            "error": f"Not a git repository: {repo_path}",
            "error_code": ToolErrorCode.NOT_GIT_REPO,
        }

    if not result["matches_expected"]:
        return {
            "success": False,
            "error": (
                f"Git scope mismatch: intended repo_path={str(Path(repo_path).resolve())} "
                f"but git root={result['git_root']}. "
                "Refusing to operate to prevent affecting files outside the intended project."
            ),
            "error_code": ToolErrorCode.SCOPE_MISMATCH,
            "git_root": result["git_root"],
        }

    logger.debug(f"Git scope validated: root={result['git_root']}")
    return None


class GitAddParams(BaseModel):
    files: List[str] = Field(
        ...,
        description="List of explicit file paths to stage. Do NOT use ['.'] — specify individual files.",
    )
    repo_path: str = Field(
        ".", description="Path to the git repository (default: current directory)"
    )


class GitAddTool(Tool):
    name = "git_add"
    description = "Stage specific files for the next git commit. You MUST list individual file paths — 'git add .' is not allowed."
    parameters_model = GitAddParams
    requires_confirmation = True
    category = "git"

    async def execute(self, files: list, repo_path: str = ".") -> Dict[str, Any]:  # type: ignore[override]
        """Stage files for git commit with safety checks."""
        try:
            if files == ["."] or files == ["*"]:
                return {
                    "success": False,
                    "error": (
                        "Refusing to stage all files ('git add .'). "
                        "Please specify explicit file paths to stage only "
                        "the files you intentionally created or modified."
                    ),
                    "error_code": ToolErrorCode.UNSAFE_STAGING,
                }

            from coderAI.system.safeguards import filter_stageable_files

            allowed, rejected = filter_stageable_files(files)

            if rejected:
                logger.info(f"git_add: filtered {len(rejected)} junk file(s): {rejected}")

            if not allowed:
                return {
                    "success": False,
                    "error": (
                        f"All requested files were filtered as junk/internal artifacts: "
                        f"{rejected}. No files staged."
                    ),
                    "error_code": ToolErrorCode.ALL_FILTERED,
                }

            # ``--`` separates paths from options so a crafted filename can't
            # inject a git flag.
            result = await _run_git_command(["add", "--"] + allowed, repo_path, needs_lock=True)
            if not result["success"]:
                return result  # scope validation error

            if result["returncode"] != 0:
                return {
                    "success": False,
                    "error": result["stderr"].decode("utf-8", errors="replace"),
                }

            out = {
                "success": True,
                "files_staged": allowed,
                "message": f"Staged {len(allowed)} file(s)",
            }
            if rejected:
                out["files_filtered"] = rejected
                out["filter_note"] = (
                    f"{len(rejected)} file(s) auto-filtered (junk/internal artifacts)"
                )
            logger.info(f"git_add: staged {allowed}")
            return out
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


class GitStatusParams(BaseModel):
    repo_path: str = Field(
        ".", description="Path to the git repository (default: current directory)"
    )


class GitStatusTool(Tool):
    name = "git_status"
    description = "Get the status of a git repository. Output truncated at 64KB."
    parameters_model = GitStatusParams
    is_read_only = True
    category = "git"

    async def execute(self, repo_path: str = ".") -> Dict[str, Any]:  # type: ignore[override]
        try:
            result = await _run_git_command(["status", "--porcelain", "-b"], repo_path)
            if not result["success"]:
                return result

            if result["returncode"] != 0:
                return {
                    "success": False,
                    "error": result["stderr"].decode("utf-8", errors="replace"),
                }

            output, truncated = _truncate_output(result["stdout"].decode("utf-8", errors="replace"))
            lines = output.strip().split("\n")
            change_lines = [line for line in lines if line and not line.startswith("##")]
            return {
                "success": True,
                "status": output,
                "has_changes": bool(change_lines),
                "truncated": truncated,
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


class GitDiffParams(BaseModel):
    repo_path: str = Field(
        ".", description="Path to the git repository (default: current directory)"
    )
    file_path: Optional[str] = Field(None, description="Optional specific file to diff")
    staged: bool = Field(False, description="Show staged changes only (default: false)")


class GitDiffTool(Tool):
    """Tool for viewing git diffs."""

    name = "git_diff"
    description = "View git diff for changes. Output truncated at 64KB; use file_path to narrow scope on huge diffs."
    parameters_model = GitDiffParams
    is_read_only = True
    category = "git"

    async def execute(  # type: ignore[override]
        self, repo_path: str = ".", file_path: Optional[str] = None, staged: bool = False
    ) -> Dict[str, Any]:  # type: ignore[override]
        try:
            args = ["diff"]
            if staged:
                args.append("--cached")
            if file_path:
                reject = _reject_option_like(file_path, "file_path")
                if reject:
                    return reject
                # ``--`` ends option parsing so the path can't become a flag.
                args.append("--")
                args.append(file_path)

            result = await _run_git_command(args, repo_path)
            if not result["success"]:
                return result

            if result["returncode"] != 0:
                return {
                    "success": False,
                    "error": result["stderr"].decode("utf-8", errors="replace"),
                }

            diff, truncated = _truncate_output(result["stdout"].decode("utf-8", errors="replace"))
            return {
                "success": True,
                "diff": diff,
                "has_diff": bool(diff.strip()),
                "truncated": truncated,
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


class GitCommitParams(BaseModel):
    message: str = Field(..., description="Commit message")
    repo_path: str = Field(
        ".", description="Path to the git repository (default: current directory)"
    )


class GitCommitTool(Tool):
    name = "git_commit"
    description = "Create a git commit with staged changes"
    parameters_model = GitCommitParams
    requires_confirmation = True
    category = "git"

    async def execute(self, message: str, repo_path: str = ".") -> Dict[str, Any]:  # type: ignore[override]
        try:
            result = await _run_git_command(["commit", "-m", message], repo_path, needs_lock=True)
            if not result["success"]:
                return result

            output = result["stdout"].decode("utf-8", errors="replace")
            error = result["stderr"].decode("utf-8", errors="replace")
            logger.info(f"git_commit: repo={repo_path} message={message!r}")

            return {
                "success": result["returncode"] == 0,
                "output": output + error,
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


class GitLogParams(BaseModel):
    repo_path: str = Field(
        ".", description="Path to the git repository (default: current directory)"
    )
    limit: int = Field(10, description="Number of commits to show (default: 10)")


class GitLogTool(Tool):
    name = "git_log"
    description = "View git commit history. Output truncated at 64KB."
    parameters_model = GitLogParams
    is_read_only = True
    category = "git"

    async def execute(self, repo_path: str = ".", limit: int = 10) -> Dict[str, Any]:  # type: ignore[override]
        try:
            if limit < 1:
                limit = 1
            elif limit > 1000:
                limit = 1000

            result = await _run_git_command(["log", "--oneline", "-n", str(limit)], repo_path)
            if not result["success"]:
                return result

            if result["returncode"] != 0:
                return {
                    "success": False,
                    "error": result["stderr"].decode("utf-8", errors="replace"),
                }

            log, truncated = _truncate_output(result["stdout"].decode("utf-8", errors="replace"))
            return {
                "success": True,
                "log": log,
                "commits": log.strip().split("\n") if log.strip() else [],
                "truncated": truncated,
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


class GitBranchParams(BaseModel):
    action: str = Field(
        ...,
        description=(
            "Action: 'list' (show branches), 'create' (new branch), 'delete' (remove branch)."
        ),
    )
    branch_name: Optional[str] = Field(
        None, description="Branch name (required for 'create' and 'delete')."
    )
    repo_path: str = Field(
        ".", description="Path to the git repository (default: current directory)"
    )


class GitBranchTool(Tool):
    name = "git_branch"
    description = "List, create, or delete git branches. Output truncated at 64KB."
    parameters_model = GitBranchParams
    requires_confirmation = True
    category = "git"

    async def execute(  # type: ignore[override]
        self, action: str, branch_name: Optional[str] = None, repo_path: str = "."
    ) -> Dict[str, Any]:  # type: ignore[override]
        try:
            args: List[str]
            if action == "list":
                args = ["branch", "-a"]
            elif action == "create":
                if not branch_name:
                    return {"success": False, "error": "branch_name is required for 'create'."}
                args = ["branch", branch_name]
            elif action == "delete":
                if not branch_name:
                    return {"success": False, "error": "branch_name is required for 'delete'."}
                args = ["branch", "-d", branch_name]
            else:
                return {"success": False, "error": f"Unknown action: {action}"}

            result = await _run_git_command(args, repo_path, needs_lock=True)
            if not result["success"]:
                return result

            if action == "list":
                if result["returncode"] != 0:
                    return {
                        "success": False,
                        "error": result["stderr"].decode("utf-8", errors="replace"),
                    }
                output, truncated = _truncate_output(
                    result["stdout"].decode("utf-8", errors="replace")
                )
                branches = [
                    b.strip().removeprefix("* ") for b in output.strip().split("\n") if b.strip()
                ]
                return {
                    "success": True,
                    "branches": branches,
                    "count": len(branches),
                    "truncated": truncated,
                }
            elif action == "create":
                if result["returncode"] != 0:
                    return {
                        "success": False,
                        "error": result["stderr"].decode("utf-8", errors="replace"),
                    }
                return {"success": True, "message": f"Branch '{branch_name}' created."}
            else:  # delete
                output = result["stdout"].decode("utf-8", errors="replace") + result[
                    "stderr"
                ].decode("utf-8", errors="replace")
                return {"success": result["returncode"] == 0, "output": output}
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


class GitCheckoutParams(BaseModel):
    branch: str = Field(..., description="Branch name or commit hash to checkout")
    create: bool = Field(False, description="Create the branch if it doesn't exist (-b flag)")
    repo_path: str = Field(
        ".", description="Path to the git repository (default: current directory)"
    )


class GitCheckoutTool(Tool):
    name = "git_checkout"
    description = "Switch to a different git branch or create and switch to a new branch"
    parameters_model = GitCheckoutParams
    requires_confirmation = True
    category = "git"

    async def execute(  # type: ignore[override]
        self, branch: str, create: bool = False, repo_path: str = "."
    ) -> Dict[str, Any]:  # type: ignore[override]
        try:
            from coderAI.system.safeguards import get_current_branch

            branch_before = await get_current_branch(repo_path)
            logger.info(
                f"git_checkout: branch_before={branch_before} "
                f"target={branch} create={create} repo={repo_path}"
            )

            args = ["checkout"]
            if create:
                args.append("-b")
            args.append(branch)

            result = await _run_git_command(args, repo_path, needs_lock=True)
            if not result["success"]:
                return result

            output = result["stdout"].decode("utf-8", errors="replace") + result["stderr"].decode(
                "utf-8", errors="replace"
            )
            return {"success": result["returncode"] == 0, "output": output.strip()}
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


class GitStashParams(BaseModel):
    action: str = Field(
        ...,
        description="Action: 'push' (stash changes), 'pop' (apply and remove top stash), 'list' (show stashes), 'drop' (remove a stash entry).",
    )
    message: Optional[str] = Field(None, description="Optional message for 'push' action.")
    stash_index: int = Field(
        0, description="Stash index for 'pop' or 'drop' (default: 0 = latest)."
    )
    repo_path: str = Field(
        ".", description="Path to the git repository (default: current directory)"
    )


class GitStashTool(Tool):
    name = "git_stash"
    description = "Stash, pop, list, or drop git stashes. Output truncated at 64KB."
    parameters_model = GitStashParams
    requires_confirmation = True
    category = "git"

    async def execute(  # type: ignore[override]
        self,
        action: str,
        message: Optional[str] = None,
        stash_index: int = 0,
        repo_path: str = ".",
    ) -> Dict[str, Any]:  # type: ignore[override]
        try:
            if action == "push":
                args = ["stash", "push"]
                if message:
                    args.extend(["-m", message])
            elif action == "pop":
                args = ["stash", "pop", f"stash@{{{stash_index}}}"]
            elif action == "list":
                args = ["stash", "list"]
            elif action == "drop":
                args = ["stash", "drop", f"stash@{{{stash_index}}}"]
            else:
                return {"success": False, "error": f"Unknown action: {action}"}

            result = await _run_git_command(args, repo_path, needs_lock=True)
            if not result["success"]:
                return result

            output = result["stdout"].decode("utf-8", errors="replace") + result["stderr"].decode(
                "utf-8", errors="replace"
            )
            return {"success": result["returncode"] == 0, "output": output.strip()}
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


class GitPushParams(BaseModel):
    remote: str = Field("origin", description="Remote name (default: origin)")
    branch: Optional[str] = Field(None, description="Branch to push (default: current branch)")
    force: bool = Field(
        False,
        description="Force push. NOTE: This uses --force-with-lease (not --force) to prevent overwriting upstream changes you haven't seen.",
    )
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

    async def execute(  # type: ignore[override]
        self,
        remote: str = "origin",
        branch: Optional[str] = None,
        force: bool = False,
        set_upstream: bool = False,
        repo_path: str = ".",
    ) -> Dict[str, Any]:  # type: ignore[override]
        try:
            args = ["push"]
            if set_upstream:
                args.append("-u")
            if force:
                args.append("--force-with-lease")
            args.append(remote)
            if branch:
                args.append(branch)

            result = await _run_git_command(args, repo_path, needs_lock=True)
            if not result["success"]:
                return result

            output = result["stdout"].decode("utf-8", errors="replace") + result["stderr"].decode(
                "utf-8", errors="replace"
            )
            return {"success": result["returncode"] == 0, "output": output.strip()}
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


class GitPullParams(BaseModel):
    remote: str = Field("origin", description="Remote name (default: origin)")
    branch: Optional[str] = Field(
        None, description="Branch to pull (default: current tracking branch)"
    )
    rebase: bool = Field(False, description="Pull with rebase instead of merge")
    repo_path: str = Field(".", description="Path to the git repository")


class GitPullTool(Tool):
    """Fetch and integrate changes from a remote repository."""

    name = "git_pull"
    description = (
        "Fetch and merge (or rebase) changes from a remote repository into the current branch."
    )
    category = "git"
    parameters_model = GitPullParams
    requires_confirmation = True

    async def execute(  # type: ignore[override]
        self,
        remote: str = "origin",
        branch: Optional[str] = None,
        rebase: bool = False,
        repo_path: str = ".",
    ) -> Dict[str, Any]:  # type: ignore[override]
        try:
            args = ["pull"]
            if rebase:
                args.append("--rebase")
            args.append(remote)
            if branch:
                args.append(branch)

            result = await _run_git_command(args, repo_path, needs_lock=True)
            if not result["success"]:
                return result

            output = result["stdout"].decode("utf-8", errors="replace") + result["stderr"].decode(
                "utf-8", errors="replace"
            )
            return {"success": result["returncode"] == 0, "output": output.strip()}
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


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

    async def execute(  # type: ignore[override]
        self,
        branch: str,
        no_ff: bool = False,
        squash: bool = False,
        message: Optional[str] = None,
        repo_path: str = ".",
    ) -> Dict[str, Any]:  # type: ignore[override]
        try:
            args = ["merge"]
            if no_ff:
                args.append("--no-ff")
            if squash:
                args.append("--squash")
            if message:
                args.extend(["-m", message])
            args.append(branch)

            result = await _run_git_command(args, repo_path, needs_lock=True)
            if not result["success"]:
                return result

            output = result["stdout"].decode("utf-8", errors="replace") + result["stderr"].decode(
                "utf-8", errors="replace"
            )
            return {"success": result["returncode"] == 0, "output": output.strip()}
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


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

    async def execute(  # type: ignore[override]
        self,
        onto: str,
        abort: bool = False,
        continue_rebase: bool = False,
        repo_path: str = ".",
    ) -> Dict[str, Any]:  # type: ignore[override]
        try:
            if abort:
                args = ["rebase", "--abort"]
            elif continue_rebase:
                args = ["rebase", "--continue"]
            else:
                args = ["rebase", onto]

            result = await _run_git_command(args, repo_path, needs_lock=True)
            if not result["success"]:
                return result

            output = result["stdout"].decode("utf-8", errors="replace") + result["stderr"].decode(
                "utf-8", errors="replace"
            )
            return {"success": result["returncode"] == 0, "output": output.strip()}
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


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

    async def execute(  # type: ignore[override]
        self,
        commit: str,
        no_commit: bool = False,
        repo_path: str = ".",
    ) -> Dict[str, Any]:  # type: ignore[override]
        try:
            args = ["revert", "--no-edit"]
            if no_commit:
                args.append("-n")
            args.append(commit)

            result = await _run_git_command(args, repo_path, needs_lock=True)
            if not result["success"]:
                return result

            output = result["stdout"].decode("utf-8", errors="replace") + result["stderr"].decode(
                "utf-8", errors="replace"
            )
            return {"success": result["returncode"] == 0, "output": output.strip()}
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


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

    async def execute(  # type: ignore[override]
        self,
        ref: str = "HEAD",
        mode: str = "mixed",
        repo_path: str = ".",
    ) -> Dict[str, Any]:  # type: ignore[override]
        try:
            if mode not in ("soft", "mixed", "hard"):
                return {
                    "success": False,
                    "error": f"Invalid mode '{mode}'. Use soft, mixed, or hard.",
                }

            result = await _run_git_command(["reset", f"--{mode}", ref], repo_path, needs_lock=True)
            if not result["success"]:
                return result

            output = result["stdout"].decode("utf-8", errors="replace") + result["stderr"].decode(
                "utf-8", errors="replace"
            )
            return {"success": result["returncode"] == 0, "output": output.strip()}
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


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

    async def execute(  # type: ignore[override]
        self,
        ref: str = "HEAD",
        stat_only: bool = False,
        repo_path: str = ".",
    ) -> Dict[str, Any]:  # type: ignore[override]
        try:
            reject = _reject_option_like(ref, "ref")
            if reject:
                return reject
            args = ["show"]
            if stat_only:
                args.append("--stat")
            # A ref cannot precede ``--`` (git would read it as a pathspec), so
            # the leading-dash reject above is what blocks ``--output=`` here.
            args.append(ref)

            result = await _run_git_command(args, repo_path, validate_scope=False)
            if not result["success"]:
                return result

            if result["returncode"] != 0:
                return {
                    "success": False,
                    "error": result["stderr"].decode("utf-8", errors="replace").strip(),
                }

            output, truncated = _truncate_output(result["stdout"].decode("utf-8", errors="replace"))
            return {"success": True, "output": output, "truncated": truncated}
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


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

    async def execute(  # type: ignore[override]
        self,
        action: str,
        name: Optional[str] = None,
        url: Optional[str] = None,
        repo_path: str = ".",
    ) -> Dict[str, Any]:  # type: ignore[override]
        try:
            if action == "list":
                args = ["remote", "-v"]
            elif action == "add":
                if not name or not url:
                    return {"success": False, "error": "'name' and 'url' required for 'add'."}
                args = ["remote", "add", name, url]
            elif action == "remove":
                if not name:
                    return {"success": False, "error": "'name' required for 'remove'."}
                args = ["remote", "remove", name]
            elif action == "set-url":
                if not name or not url:
                    return {
                        "success": False,
                        "error": "'name' and 'url' required for 'set-url'.",
                    }
                args = ["remote", "set-url", name, url]
            else:
                return {"success": False, "error": f"Unknown action: {action}"}

            result = await _run_git_command(args, repo_path, needs_lock=True)
            if not result["success"]:
                return result

            output = result["stdout"].decode("utf-8", errors="replace") + result["stderr"].decode(
                "utf-8", errors="replace"
            )
            return {"success": result["returncode"] == 0, "output": output.strip()}
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


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

    async def execute(  # type: ignore[override]
        self,
        file_path: str,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        repo_path: str = ".",
    ) -> Dict[str, Any]:  # type: ignore[override]
        try:
            reject = _reject_option_like(file_path, "file_path")
            if reject:
                return reject
            args = ["blame", "--porcelain"]
            if start_line is not None and end_line is not None:
                args.extend([f"-L{start_line},{end_line}"])
            elif start_line is not None:
                args.extend([f"-L{start_line},+30"])
            # ``--`` ends option parsing so the path can't become a flag.
            args.append("--")
            args.append(file_path)

            result = await _run_git_command(args, repo_path, validate_scope=False)
            if not result["success"]:
                return result

            if result["returncode"] != 0:
                return {
                    "success": False,
                    "error": result["stderr"].decode("utf-8", errors="replace").strip(),
                }

            raw, truncated = _truncate_output(result["stdout"].decode("utf-8", errors="replace"))
            lines: List[Dict[str, str]] = []
            current: Dict[str, str] = {}
            for line in raw.splitlines():
                if line.startswith("\t"):
                    current["code"] = line[1:]
                    lines.append(current)
                    current = {}
                elif " " in line:
                    parts = line.split(" ", 3)
                    if len(parts[0]) >= 40 and all(c in "0123456789abcdef" for c in parts[0]):
                        current["commit"] = parts[0]
                        if len(parts) >= 3:
                            current["line"] = parts[2]
                    else:
                        key, _, value = line.partition(" ")
                        current[key] = value

            return {
                "success": True,
                "file": file_path,
                "annotations": lines,
                "count": len(lines),
                "truncated": truncated,
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


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

    async def execute(  # type: ignore[override]
        self,
        commits: List[str],
        no_commit: bool = False,
        repo_path: str = ".",
    ) -> Dict[str, Any]:  # type: ignore[override]
        try:
            args = ["cherry-pick"]
            if no_commit:
                args.append("-n")
            args.extend(commits)

            result = await _run_git_command(args, repo_path, needs_lock=True)
            if not result["success"]:
                return result

            output = result["stdout"].decode("utf-8", errors="replace") + result["stderr"].decode(
                "utf-8", errors="replace"
            )
            return {"success": result["returncode"] == 0, "output": output.strip()}
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


class GitTagParams(BaseModel):
    action: str = Field(
        ...,
        description="Action: 'list' (show tags), 'create' (add tag), 'delete' (remove tag).",
    )
    tag_name: Optional[str] = Field(None, description="Tag name (required for create/delete)")
    message: Optional[str] = Field(
        None, description="Annotated tag message (omit for lightweight tag)"
    )
    ref: str = Field("HEAD", description="Commit to tag (default: HEAD, only for create)")
    repo_path: str = Field(".", description="Path to the git repository")


class GitTagTool(Tool):
    """List, create, or delete git tags."""

    name = "git_tag"
    description = (
        "Manage git tags: list existing tags, create lightweight or annotated tags, or delete tags."
    )
    category = "git"
    parameters_model = GitTagParams
    requires_confirmation = True

    async def execute(  # type: ignore[override]
        self,
        action: str,
        tag_name: Optional[str] = None,
        message: Optional[str] = None,
        ref: str = "HEAD",
        repo_path: str = ".",
    ) -> Dict[str, Any]:  # type: ignore[override]
        try:
            if action == "list":
                args = ["tag", "--list", "--sort=-version:refname"]
            elif action == "create":
                if not tag_name:
                    return {"success": False, "error": "'tag_name' required for create."}
                if message:
                    args = ["tag", "-a", tag_name, "-m", message, ref]
                else:
                    args = ["tag", tag_name, ref]
            elif action == "delete":
                if not tag_name:
                    return {"success": False, "error": "'tag_name' required for delete."}
                args = ["tag", "-d", tag_name]
            else:
                return {"success": False, "error": f"Unknown action: {action}"}

            result = await _run_git_command(args, repo_path, needs_lock=True)
            if not result["success"]:
                return result

            output = result["stdout"].decode("utf-8", errors="replace")
            err = result["stderr"].decode("utf-8", errors="replace")
            if action == "list":
                tags = [t for t in output.strip().splitlines() if t]
                return {"success": True, "tags": tags, "count": len(tags)}
            return {
                "success": result["returncode"] == 0,
                "output": (output + err).strip(),
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


class GitFetchParams(BaseModel):
    remote: str = Field("origin", description="Remote name (default: origin)")
    branch: Optional[str] = Field(None, description="Specific branch to fetch (default: all)")
    prune: bool = Field(
        False, description="Remove remote-tracking branches that no longer exist on the remote (-p)"
    )
    repo_path: str = Field(".", description="Path to the git repository")


class GitFetchTool(Tool):
    """Download objects and refs from a remote repository without merging."""

    name = "git_fetch"
    description = "Download objects and refs from a remote repository. Unlike git_pull, this does not merge changes."
    category = "git"
    parameters_model = GitFetchParams
    is_read_only = False  # --prune deletes remote-tracking branches, so this is not read-only
    requires_confirmation = True

    async def execute(  # type: ignore[override]
        self,
        remote: str = "origin",
        branch: Optional[str] = None,
        prune: bool = False,
        repo_path: str = ".",
    ) -> Dict[str, Any]:  # type: ignore[override]
        try:
            args = ["fetch", remote]
            if prune:
                args.append("--prune")
            if branch:
                args.append(branch)

            result = await _run_git_command(args, repo_path, needs_lock=True)
            if not result["success"]:
                return result

            output = result["stdout"].decode("utf-8", errors="replace") + result["stderr"].decode(
                "utf-8", errors="replace"
            )
            return {"success": result["returncode"] == 0, "output": output.strip()}
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }
