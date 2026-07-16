"""Core git tools for everyday version control operations.

Native tools: ``git_status``, ``git_diff``, ``git_add``, ``git_commit``,
``git_log``, ``git_branch``. Rarely used ops (push/pull/merge/rebase/…) live
in :mod:`coderAI.tools.git_extended` and are exposed via the bundled
``git_extended`` MCP server.
"""

import functools
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar, cast

from pydantic import BaseModel, Field

from coderAI.tools.base import Tool
from coderAI.types.tool_error_codes import ToolErrorCode
from coderAI.system.locks import get_lock_manager
from coderAI.system.proc import run_scrubbed, subprocess_timeout
from coderAI.system.safeguards import truncate_output

logger = logging.getLogger(__name__)

MAX_GIT_OUTPUT_CHARS = 64_000

# Network git operations (push/pull/fetch) get a wider subprocess timeout than
# the config-driven local default — a slow remote is normal, an unbounded hang
# is not. The owning tools set ``timeout = GIT_NETWORK_TIMEOUT_SECONDS + 10``
# so the executor's outer cap stays behind the inner group-kill cleanup.
GIT_NETWORK_TIMEOUT_SECONDS = 300.0

_GIT_TRUNCATION_MARKER = "... [truncated {omitted} chars — re-run with a narrower scope] ..."

_F = TypeVar("_F", bound=Callable[..., Awaitable[Dict[str, Any]]])


def _tool_errors(fn: _F) -> _F:
    """Turn unexpected exceptions from ``execute()`` into a TOOL_ERROR result."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }

    return cast(_F, wrapper)


def _truncate_output(text: str, max_chars: int = MAX_GIT_OUTPUT_CHARS) -> tuple[str, bool]:
    """Truncate git output via the shared helper, returning (text, was_truncated)."""
    return truncate_output(
        text, max_chars=max_chars, mode="head_tail", marker=_GIT_TRUNCATION_MARKER
    )


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _stderr_error(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Error dict when a completed git command exited non-zero, else None."""
    if result["returncode"] != 0:
        return {"success": False, "error": _decode(result["stderr"]).strip()}
    return None


def _simple_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Shape a completed git command as ``{"success", "output"}`` (stdout+stderr)."""
    output = _decode(result["stdout"]) + _decode(result["stderr"])
    return {"success": result["returncode"] == 0, "output": output.strip()}


async def _run_simple(
    args: List[str],
    repo_path: str,
    *,
    needs_lock: bool = True,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """Run a git command and shape it with :func:`_simple_result`."""
    result = await _run_git_command(args, repo_path, needs_lock=needs_lock, timeout=timeout)
    if not result["success"]:
        return result
    return _simple_result(result)


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
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """Run a git command with scope validation and optional lock acquisition.

    Returns ``{"success": False, ...}`` on scope validation error or timeout,
    or ``{"success": True, "returncode": int, "stdout": bytes, "stderr": bytes}``
    on command completion.  The caller is responsible for checking returncode
    and formatting the tool-level result.

    *timeout* defaults to ``config.subprocess_timeout_seconds`` (git previously
    ran unbounded); network operations pass :data:`GIT_NETWORK_TIMEOUT_SECONDS`.
    """
    if validate_scope:
        scope_error = await _validate_git_scope(repo_path)
        if scope_error:
            return scope_error

    if timeout is None:
        timeout = subprocess_timeout()

    async def _exec():
        # Scrub secrets from the child env: git subcommands can shell out
        # (hooks, credential helpers, ``git config alias.* = !sh -c …``), so an
        # inherited ``$ANTHROPIC_API_KEY`` etc. must not be reachable. scrub_env
        # is a denylist, so HOME/PATH/GIT_* the user set survive untouched.
        returncode, stdout, stderr, timed_out = await run_scrubbed(
            ["git", *args], cwd=repo_path, shell=False, timeout=timeout
        )
        return returncode, stdout, stderr, timed_out

    if needs_lock:
        async with get_lock_manager().git_lock():
            returncode, stdout, stderr, timed_out = await _exec()
    else:
        returncode, stdout, stderr, timed_out = await _exec()

    if timed_out:
        return {
            "success": False,
            "error": f"git {args[0] if args else ''} timed out after {timeout:.0f}s",
            "error_code": ToolErrorCode.TIMEOUT,
        }

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

    @_tool_errors
    async def execute(self, files: list, repo_path: str = ".") -> Dict[str, Any]:  # type: ignore[override]
        """Stage files for git commit with safety checks."""
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

        error = _stderr_error(result)
        if error:
            return error

        out = {
            "success": True,
            "files_staged": allowed,
            "message": f"Staged {len(allowed)} file(s)",
        }
        if rejected:
            out["files_filtered"] = rejected
            out["filter_note"] = f"{len(rejected)} file(s) auto-filtered (junk/internal artifacts)"
        logger.info(f"git_add: staged {allowed}")
        return out


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

    @_tool_errors
    async def execute(self, repo_path: str = ".") -> Dict[str, Any]:  # type: ignore[override]
        result = await _run_git_command(["status", "--porcelain", "-b"], repo_path)
        if not result["success"]:
            return result

        error = _stderr_error(result)
        if error:
            return error

        output, truncated = _truncate_output(_decode(result["stdout"]))
        lines = output.strip().split("\n")
        change_lines = [line for line in lines if line and not line.startswith("##")]
        return {
            "success": True,
            "status": output,
            "has_changes": bool(change_lines),
            "truncated": truncated,
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

    @_tool_errors
    async def execute(  # type: ignore[override]
        self, repo_path: str = ".", file_path: Optional[str] = None, staged: bool = False
    ) -> Dict[str, Any]:
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

        error = _stderr_error(result)
        if error:
            return error

        diff, truncated = _truncate_output(_decode(result["stdout"]))
        return {
            "success": True,
            "diff": diff,
            "has_diff": bool(diff.strip()),
            "truncated": truncated,
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

    @_tool_errors
    async def execute(self, message: str, repo_path: str = ".") -> Dict[str, Any]:  # type: ignore[override]
        result = await _run_git_command(["commit", "-m", message], repo_path, needs_lock=True)
        if not result["success"]:
            return result

        logger.info(f"git_commit: repo={repo_path} message={message!r}")
        return _simple_result(result)


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

    @_tool_errors
    async def execute(self, repo_path: str = ".", limit: int = 10) -> Dict[str, Any]:  # type: ignore[override]
        limit = max(1, min(limit, 1000))

        result = await _run_git_command(["log", "--oneline", "-n", str(limit)], repo_path)
        if not result["success"]:
            return result

        error = _stderr_error(result)
        if error:
            return error

        log, truncated = _truncate_output(_decode(result["stdout"]))
        return {
            "success": True,
            "log": log,
            "commits": log.strip().split("\n") if log.strip() else [],
            "truncated": truncated,
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

    @_tool_errors
    async def execute(  # type: ignore[override]
        self, action: str, branch_name: Optional[str] = None, repo_path: str = "."
    ) -> Dict[str, Any]:
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
            error = _stderr_error(result)
            if error:
                return error
            output, truncated = _truncate_output(_decode(result["stdout"]))
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
            error = _stderr_error(result)
            if error:
                return error
            return {"success": True, "message": f"Branch '{branch_name}' created."}
        else:  # delete
            return _simple_result(result)
