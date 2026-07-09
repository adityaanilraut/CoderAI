"""Extended (rarely used) git tools — served via the bundled ``git_extended`` MCP server.

These are intentionally **not** auto-registered as native agent tools. Everyday
git ops stay in :mod:`coderAI.tools.git`; connect the MCP server (auto on
startup) to get ``mcp__git_extended__git_*`` for the long tail.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from coderAI.tools.base import Tool
from coderAI.tools.git import (
    GIT_NETWORK_TIMEOUT_SECONDS,
    _decode,
    _reject_option_like,
    _run_git_command,
    _run_simple,
    _simple_result,
    _stderr_error,
    _tool_errors,
    _truncate_output,
)

logger = logging.getLogger(__name__)


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

    @_tool_errors
    async def execute(  # type: ignore[override]
        self, branch: str, create: bool = False, repo_path: str = "."
    ) -> Dict[str, Any]:
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

        return await _run_simple(args, repo_path)


class GitStashParams(BaseModel):
    action: str = Field(
        ...,
        description=(
            "Action: 'push' (stash changes), 'pop' (apply and remove top stash), "
            "'list' (show stashes), 'drop' (remove a stash entry)."
        ),
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

    @_tool_errors
    async def execute(  # type: ignore[override]
        self,
        action: str,
        message: Optional[str] = None,
        stash_index: int = 0,
        repo_path: str = ".",
    ) -> Dict[str, Any]:
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

        return await _run_simple(args, repo_path)


class GitPushParams(BaseModel):
    remote: str = Field("origin", description="Remote name (default: origin)")
    branch: Optional[str] = Field(None, description="Branch to push (default: current branch)")
    force: bool = Field(
        False,
        description=(
            "Force push. NOTE: This uses --force-with-lease (not --force) to prevent "
            "overwriting upstream changes you haven't seen."
        ),
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
    timeout = GIT_NETWORK_TIMEOUT_SECONDS + 10.0

    @_tool_errors
    async def execute(  # type: ignore[override]
        self,
        remote: str = "origin",
        branch: Optional[str] = None,
        force: bool = False,
        set_upstream: bool = False,
        repo_path: str = ".",
    ) -> Dict[str, Any]:
        args = ["push"]
        if set_upstream:
            args.append("-u")
        if force:
            args.append("--force-with-lease")
        args.append(remote)
        if branch:
            args.append(branch)

        return await _run_simple(args, repo_path, timeout=GIT_NETWORK_TIMEOUT_SECONDS)


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
    timeout = GIT_NETWORK_TIMEOUT_SECONDS + 10.0

    @_tool_errors
    async def execute(  # type: ignore[override]
        self,
        remote: str = "origin",
        branch: Optional[str] = None,
        rebase: bool = False,
        repo_path: str = ".",
    ) -> Dict[str, Any]:
        args = ["pull"]
        if rebase:
            args.append("--rebase")
        args.append(remote)
        if branch:
            args.append(branch)

        return await _run_simple(args, repo_path, timeout=GIT_NETWORK_TIMEOUT_SECONDS)


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

    @_tool_errors
    async def execute(  # type: ignore[override]
        self,
        branch: str,
        no_ff: bool = False,
        squash: bool = False,
        message: Optional[str] = None,
        repo_path: str = ".",
    ) -> Dict[str, Any]:
        args = ["merge"]
        if no_ff:
            args.append("--no-ff")
        if squash:
            args.append("--squash")
        if message:
            args.extend(["-m", message])
        args.append(branch)

        return await _run_simple(args, repo_path)


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

    @_tool_errors
    async def execute(  # type: ignore[override]
        self,
        onto: str,
        abort: bool = False,
        continue_rebase: bool = False,
        repo_path: str = ".",
    ) -> Dict[str, Any]:
        if abort:
            args = ["rebase", "--abort"]
        elif continue_rebase:
            args = ["rebase", "--continue"]
        else:
            args = ["rebase", onto]

        return await _run_simple(args, repo_path)


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

    @_tool_errors
    async def execute(  # type: ignore[override]
        self,
        commit: str,
        no_commit: bool = False,
        repo_path: str = ".",
    ) -> Dict[str, Any]:
        args = ["revert", "--no-edit"]
        if no_commit:
            args.append("-n")
        args.append(commit)

        return await _run_simple(args, repo_path)


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

    @_tool_errors
    async def execute(  # type: ignore[override]
        self,
        ref: str = "HEAD",
        mode: str = "mixed",
        repo_path: str = ".",
    ) -> Dict[str, Any]:
        if mode not in ("soft", "mixed", "hard"):
            return {
                "success": False,
                "error": f"Invalid mode '{mode}'. Use soft, mixed, or hard.",
            }

        return await _run_simple(["reset", f"--{mode}", ref], repo_path)


class GitShowParams(BaseModel):
    ref: str = Field("HEAD", description="Commit hash, tag, or branch to inspect (default: HEAD)")
    stat_only: bool = Field(False, description="Show only file change stats, not the full diff")
    repo_path: str = Field(".", description="Path to the git repository")


class GitShowTool(Tool):
    """Display details and diff of a specific commit."""

    name = "git_show"
    description = (
        "Show the commit message, author, date, and diff for a specific commit or "
        "reference. Output truncated at 64KB."
    )
    category = "git"
    parameters_model = GitShowParams
    is_read_only = True

    @_tool_errors
    async def execute(  # type: ignore[override]
        self,
        ref: str = "HEAD",
        stat_only: bool = False,
        repo_path: str = ".",
    ) -> Dict[str, Any]:
        reject = _reject_option_like(ref, "ref")
        if reject:
            return reject
        args = ["show"]
        if stat_only:
            args.append("--stat")
        args.append(ref)

        result = await _run_git_command(args, repo_path, validate_scope=False)
        if not result["success"]:
            return result

        error = _stderr_error(result)
        if error:
            return error

        output, truncated = _truncate_output(_decode(result["stdout"]))
        return {"success": True, "output": output, "truncated": truncated}


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

    @_tool_errors
    async def execute(  # type: ignore[override]
        self,
        action: str,
        name: Optional[str] = None,
        url: Optional[str] = None,
        repo_path: str = ".",
    ) -> Dict[str, Any]:
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

        return await _run_simple(args, repo_path)


class GitBlameParams(BaseModel):
    file_path: str = Field(..., description="File path to annotate")
    start_line: Optional[int] = Field(None, description="First line of the range to blame")
    end_line: Optional[int] = Field(None, description="Last line of the range to blame")
    repo_path: str = Field(".", description="Path to the git repository")


class GitBlameTool(Tool):
    """Show which commit last modified each line of a file."""

    name = "git_blame"
    description = (
        "Annotate each line of a file with the commit and author that last changed it. "
        "Output truncated at 64KB."
    )
    category = "git"
    parameters_model = GitBlameParams
    is_read_only = True

    @_tool_errors
    async def execute(  # type: ignore[override]
        self,
        file_path: str,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        repo_path: str = ".",
    ) -> Dict[str, Any]:
        reject = _reject_option_like(file_path, "file_path")
        if reject:
            return reject
        args = ["blame", "--porcelain"]
        if start_line is not None and end_line is not None:
            args.extend([f"-L{start_line},{end_line}"])
        elif start_line is not None:
            args.extend([f"-L{start_line},+30"])
        args.append("--")
        args.append(file_path)

        result = await _run_git_command(args, repo_path, validate_scope=False)
        if not result["success"]:
            return result

        error = _stderr_error(result)
        if error:
            return error

        raw, truncated = _truncate_output(_decode(result["stdout"]))
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

    @_tool_errors
    async def execute(  # type: ignore[override]
        self,
        commits: List[str],
        no_commit: bool = False,
        repo_path: str = ".",
    ) -> Dict[str, Any]:
        args = ["cherry-pick"]
        if no_commit:
            args.append("-n")
        args.extend(commits)

        return await _run_simple(args, repo_path)


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

    @_tool_errors
    async def execute(  # type: ignore[override]
        self,
        action: str,
        tag_name: Optional[str] = None,
        message: Optional[str] = None,
        ref: str = "HEAD",
        repo_path: str = ".",
    ) -> Dict[str, Any]:
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

        if action == "list":
            tags = [t for t in _decode(result["stdout"]).strip().splitlines() if t]
            return {"success": True, "tags": tags, "count": len(tags)}
        return _simple_result(result)


class GitFetchParams(BaseModel):
    remote: str = Field("origin", description="Remote name (default: origin)")
    branch: Optional[str] = Field(None, description="Specific branch to fetch (default: all)")
    prune: bool = Field(
        False,
        description="Remove remote-tracking branches that no longer exist on the remote (-p)",
    )
    repo_path: str = Field(".", description="Path to the git repository")


class GitFetchTool(Tool):
    """Download objects and refs from a remote repository without merging."""

    name = "git_fetch"
    description = (
        "Download objects and refs from a remote repository. Unlike git_pull, "
        "this does not merge changes."
    )
    category = "git"
    parameters_model = GitFetchParams
    is_read_only = False  # --prune deletes remote-tracking branches
    requires_confirmation = True
    timeout = GIT_NETWORK_TIMEOUT_SECONDS + 10.0

    @_tool_errors
    async def execute(  # type: ignore[override]
        self,
        remote: str = "origin",
        branch: Optional[str] = None,
        prune: bool = False,
        repo_path: str = ".",
    ) -> Dict[str, Any]:
        args = ["fetch", remote]
        if prune:
            args.append("--prune")
        if branch:
            args.append(branch)

        return await _run_simple(args, repo_path, timeout=GIT_NETWORK_TIMEOUT_SECONDS)


# Tools exposed by the bundled git_extended MCP server (order is stable for tests).
EXTENDED_GIT_TOOLS: List[Tool] = [
    GitCheckoutTool(),
    GitStashTool(),
    GitPushTool(),
    GitPullTool(),
    GitMergeTool(),
    GitRebaseTool(),
    GitRevertTool(),
    GitResetTool(),
    GitShowTool(),
    GitRemoteTool(),
    GitBlameTool(),
    GitCherryPickTool(),
    GitTagTool(),
    GitFetchTool(),
]
