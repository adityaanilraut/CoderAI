# Split from the former combined search module; grep now lives in coderai/tools/file/grep.py.
"""First-class glob tool backed by bundled ripgrep.

Cap inline results, and spill the complete formatted page when over cap. A Python
fallback is used when the binary is unavailable so hosts do not require `rg`.
"""

from __future__ import annotations

import pathlib
import time
from typing import Any

from coderai.spill import SpillRef, try_save_text
from coderai.tools.file._search_common import (
    GLOB_VCS_EXCLUDES,
    SEARCH_TIMEOUT_MS,
    RipgrepRun,
    SearchError,
    _iter_workspace_files,
    _matches_glob,
    _prefer_python_backend,
    _search_error_result,
    _session_id,
    _session_workdir,
    resolve_rg_path,
    run_ripgrep,
    to_workdir_relative,
)
from coderai.tools.legacy.schema import define_tool
from coderai.tools.legacy.types import (
    ToolDefinition,
    ToolExecutionContext,
    ToolResult,
    as_str,
)

# Backwards-compatible re-exports: grep lived in this module before the split.
# New code should import from coderai.tools.file.grep directly.
from coderai.tools.file.grep import GREP_DESCRIPTION
from coderai.tools.file.grep import GREP_MAX_LINE_BYTES
from coderai.tools.file.grep import GREP_MAX_MATCHES
from coderai.tools.file.grep import GrepMatch
from coderai.tools.file.grep import build_grep_command
from coderai.tools.file.grep import format_grep_matches
from coderai.tools.file.grep import format_grep_output
from coderai.tools.file.grep import handle_grep
from coderai.tools.file.grep import handle_grep_tool
from coderai.tools.file.grep import parse_grep_args
from coderai.tools.file.grep import parse_grep_matches
from coderai.tools.file.grep import preview_line

__all__ = (
    "GLOB_DESCRIPTION",
    "GLOB_MAX_RESULTS",
    "GREP_DESCRIPTION",
    "GREP_MAX_LINE_BYTES",
    "GREP_MAX_MATCHES",
    "GrepMatch",
    "RipgrepRun",
    "SearchError",
    "build_glob_command",
    "build_grep_command",
    "format_grep_matches",
    "format_grep_output",
    "glob_tool_definition",
    "handle_glob",
    "handle_glob_tool",
    "handle_grep",
    "handle_grep_tool",
    "parse_glob_args",
    "parse_grep_args",
    "parse_grep_matches",
    "preview_line",
    "render_glob_paths",
    "resolve_rg_path",
    "run_ripgrep",
    "sample_across_top_level",
    "to_workdir_relative",
)

GLOB_MAX_RESULTS = 100

GLOB_DESCRIPTION = (
    "Find files whose paths match a glob pattern. Returns matching file paths — never directories — "
    "including hidden and ignored files (VCS metadata directories are excluded). "
    f"Up to {GLOB_MAX_RESULTS} paths come back in modification-time order; a larger result instead returns "
    f"{GLOB_MAX_RESULTS} paths sampled across top-level entries, says so, and reports where the complete "
    "sorted list was saved. This tool does not enumerate directory entries."
)


def parse_glob_args(args: dict[str, Any]) -> dict[str, str]:
    pattern = as_str(args.get("pattern"))
    if not pattern.strip():
        raise ValueError("pattern must be a non-empty string")
    path = args.get("path")
    if path is not None:
        path_s = as_str(path)
        if not path_s.strip():
            raise ValueError("path must be a non-empty string when given")
        return {"pattern": pattern, "path": path_s}
    return {"pattern": pattern}


def build_glob_command(pattern: str, path: str | None = None) -> list[str]:
    parts = [
        "--files",
        f"--glob={pattern}",
        "--sort=modified",
        "--no-ignore",
        "--hidden",
    ]
    for name in GLOB_VCS_EXCLUDES:
        parts.append(f"--glob=!**/{name}")
        parts.append(f"--glob=!**/{name}/**")
    if path is not None:
        parts.extend(["--", path])
    return parts


def sample_across_top_level(paths: list[str], max_items: int, root: str = ".") -> dict[str, Any]:
    """Round-robin sample an over-cap glob page across top-level entries."""

    def relative_to_root(path: str) -> str:
        if root in (".", ""):
            return path[2:] if path.startswith("./") else path
        trimmed = root.rstrip("/")
        if path == trimmed:
            return ""
        prefix = trimmed + "/"
        if path.startswith(prefix):
            return path[len(prefix) :]
        return path.lstrip("/")

    def top_level(path: str) -> str:
        rel = relative_to_root(path).lstrip("/")
        cut = rel.find("/")
        return rel if cut == -1 else rel[:cut]

    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for path in paths:
        key = top_level(path)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(path)

    taken: dict[str, list[str]] = {k: [] for k in order}
    indices = {k: 0 for k in order}
    count = 0
    active = list(order)
    while active and count < max_items:
        next_active: list[str] = []
        for key in active:
            if count >= max_items:
                break
            items = groups[key]
            idx = indices[key]
            taken[key].append(items[idx])
            indices[key] = idx + 1
            count += 1
            if indices[key] < len(items):
                next_active.append(key)
        active = next_active

    items = [p for k in order for p in taken[k]]
    shown = sum(1 for k in order if taken[k])
    return {"items": items, "shown": shown, "total": len(groups)}


def format_glob_page(
    items: list[str],
    seen: int,
    spill_ref: SpillRef | None,
    basis: str = ".",
) -> str:
    body = "\n".join(items)
    if spill_ref is not None:
        recovery = f"Full sorted result stored at: {spill_ref.locator}. {spill_ref.retrieval_hint}"
    else:
        recovery = "The complete result could not be saved; narrow pattern or path to see more."
    return f"{body}\n\n(Showing {len(items)} of {seen} paths{basis} {recovery})"


def render_glob_paths(
    paths: list[str],
    *,
    root: str = ".",
    max_results: int = GLOB_MAX_RESULTS,
    sample: bool = True,
    spill_ref: SpillRef | None = None,
) -> str:
    if not paths:
        return "No files found"
    if len(paths) <= max_results:
        return "\n".join(paths)
    if not sample:
        return format_glob_page(paths[:max_results], len(paths), spill_ref, ".")
    sampled = sample_across_top_level(paths, max_results, root)
    if sampled["total"] == len(paths):
        basis = "."
    else:
        extra = ""
        if sampled["shown"] < sampled["total"]:
            extra = " Narrow path to inspect a specific subtree."
        basis = (
            f", sampled across {sampled['shown']} of the {sampled['total']} "
            f"top-level entries this pattern matched instead of taken in modification-time order.{extra}"
        )
    return format_glob_page(sampled["items"], len(paths), spill_ref, basis)


def _python_glob(pattern: str, workdir: str, search_path: str | None, timeout_ms: int) -> list[str]:
    root = pathlib.Path(search_path or workdir)
    if not root.is_absolute():
        root = pathlib.Path(workdir) / root
    root = root.resolve()
    if not root.exists():
        raise SearchError(f"glob search failed: path not found: {root}", "SEARCH_FAILED")
    if root.is_file():
        rel = to_workdir_relative(str(root), workdir)
        return [rel] if _matches_glob(pathlib.Path(rel).name, pattern) else []
    deadline = time.time() + max(0.1, timeout_ms / 1000.0)
    scored: list[tuple[float, str]] = []
    for path in _iter_workspace_files(root, deadline):
        try:
            rel = to_workdir_relative(str(path), workdir)
            rel_posix = rel.replace("\\", "/")
            if _matches_glob(rel_posix, pattern):
                mtime = path.stat().st_mtime
                scored.append((mtime, rel_posix))
        except OSError:
            continue
    scored.sort(key=lambda item: item[0])  # oldest first, matching rg --sort=modified
    return [p for _, p in scored]


def handle_glob_tool(args: dict[str, Any], context: ToolExecutionContext | Any) -> ToolResult:
    try:
        parsed = parse_glob_args(args)
    except ValueError as exc:
        return ToolResult(ok=False, name="glob", error=str(exc))
    workdir = _session_workdir(context)
    pattern = parsed["pattern"]
    path = parsed.get("path")
    try:
        if _prefer_python_backend() or resolve_rg_path() is None:
            paths = _python_glob(pattern, workdir, path, SEARCH_TIMEOUT_MS)
        else:
            run = run_ripgrep(build_glob_command(pattern, path), workdir, tool_name="glob")
            if run.no_matches:
                paths = []
            else:
                paths = [
                    to_workdir_relative(line, run.workdir)
                    for line in run.stdout.split("\n")
                    if line
                ]
    except SearchError as err:
        return _search_error_result("glob", err)

    root = "." if path is None else to_workdir_relative(path, workdir)
    spill_ref = None
    if len(paths) > GLOB_MAX_RESULTS:
        spill_ref = try_save_text(
            session_id=_session_id(context),
            suggested_name="glob-results.txt",
            content="\n".join(paths),
        )
    output = render_glob_paths(paths, root=root, spill_ref=spill_ref)
    return ToolResult(
        ok=True,
        name="glob",
        output=output,
        metadata={
            "root": root,
            "paths": paths,
            "count": len(paths),
            "spill": spill_ref.to_dict() if spill_ref else None,
        },
    )


handle_glob = handle_glob_tool


def glob_tool_definition() -> ToolDefinition:
    """Per-tool registry definition for `glob` (keeps `registry.py` thin)."""
    return define_tool(
        name="glob",
        description=GLOB_DESCRIPTION,
        parameters={
            "pattern": {
                "type": "string",
                "description": (
                    'Glob pattern to match file paths against (e.g. "**/*.ts", "src/**/*.test.js"). '
                    'A pattern with no "/" matches the basename at any depth, so "*" and "*.ts" both search the whole tree; include a separator to anchor the depth.'
                ),
            },
            "path": {
                "type": "string",
                "description": "Directory to search in. Defaults to the session workspace; a relative path resolves against it.",
            },
        },
        required=["pattern"],
        handler=handle_glob_tool,
        category="filesystem",
        is_mutating=False,
        is_concurrency_safe=True,
    )


# --- CallableTool2 Implementation ---

from pathlib import Path as _Path
from kaos.path import KaosPath as _KaosPath
from kosong.tooling import CallableTool2 as _CallableTool2, ToolError as _ToolError, ToolOk as _ToolOk, ToolReturnValue as _ToolReturnValue
from pydantic import BaseModel as _BaseModel, Field as _Field

from coderai.soul.agent import Runtime as _Runtime
from coderai.tools.utils import load_desc as _load_desc
from coderai.utils.logging import logger as _logger
from coderai.utils.path import (
    is_within_directory as _is_within_directory,
    is_within_workspace as _is_within_workspace,
    kaos_path_from_user_input as _kaos_path_from_user_input,
    list_directory as _list_directory,
)

MAX_GLOB_MATCHES = 1000
GLOB_DESC_PATH = _Path(__file__).parent / "glob.md"


def _glob_description_for_os(os_kind: str) -> str:
    return _load_desc(
        GLOB_DESC_PATH,
        {
            "MAX_MATCHES": str(MAX_GLOB_MATCHES),
            "WINDOWS_PATH_HINT": "",
        },
    )


class GlobParams(_BaseModel):
    pattern: str = _Field(description="Glob pattern to match files/directories.")
    directory: str | None = _Field(
        description="Absolute path to the directory to search in (defaults to working directory).",
        default=None,
    )
    include_dirs: bool = _Field(
        description="Whether to include directories in results.",
        default=True,
    )


class Glob(_CallableTool2[GlobParams]):
    name: str = "Glob"
    description: str = _glob_description_for_os("")
    params: type[GlobParams] = GlobParams

    def __init__(self, runtime: _Runtime) -> None:
        env = getattr(runtime, "environment", None)
        os_kind = getattr(env, "os_kind", "")
        super().__init__(description=_glob_description_for_os(os_kind))
        builtin = getattr(runtime, "builtin_args", None)
        self._work_dir = getattr(builtin, "CODERAI_WORK_DIR", _KaosPath.cwd())
        self._additional_dirs = getattr(runtime, "additional_dirs", [])
        self._skills_dirs = getattr(runtime, "skills_dirs", [])

    async def _validate_pattern(self, pattern: str) -> _ToolError | None:
        if pattern.startswith("**"):
            ls_result = await _list_directory(self._work_dir)
            return _ToolError(
                output=ls_result,
                message=(
                    f"Pattern `{pattern}` starts with '**' which is not allowed. "
                    "This would recursively search all directories and may include large "
                    "directories like `node_modules`. Use more specific patterns instead."
                ),
                brief="Unsafe pattern",
            )
        return None

    async def _validate_directory(self, directory: _KaosPath) -> _ToolError | None:
        resolved_dir = directory.canonical()
        if _is_within_workspace(resolved_dir, self._work_dir, self._additional_dirs):
            return None
        if any(_is_within_directory(resolved_dir, d) for d in self._skills_dirs):
            return None
        return _ToolError(
            message=(
                f"`{directory}` is outside the workspace. "
                "You can only search within the working directory, "
                "additional directories, and skills directories."
            ),
            brief="Directory outside workspace",
        )

    async def __call__(self, params: GlobParams) -> _ToolReturnValue:
        try:
            pattern_error = await self._validate_pattern(params.pattern)
            if pattern_error:
                return pattern_error

            dir_path = (
                _kaos_path_from_user_input(params.directory) if params.directory else self._work_dir
            )

            if not dir_path.is_absolute():
                return _ToolError(
                    message=f"`{params.directory}` is not an absolute path.",
                    brief="Invalid directory",
                )

            dir_error = await self._validate_directory(dir_path)
            if dir_error:
                return dir_error

            if not await dir_path.exists():
                return _ToolError(
                    message=f"`{params.directory}` does not exist.",
                    brief="Directory not found",
                )
            if not await dir_path.is_dir():
                return _ToolError(
                    message=f"`{params.directory}` is not a directory.",
                    brief="Invalid directory",
                )

            matches: list[_KaosPath] = []
            async for match in dir_path.glob(params.pattern):
                matches.append(match)

            if not params.include_dirs:
                matches = [p for p in matches if await p.is_file()]

            matches.sort()

            message = (
                f"Found {len(matches)} matches for pattern `{params.pattern}`."
                if len(matches) > 0
                else f"No matches found for pattern `{params.pattern}`."
            )
            if len(matches) > MAX_GLOB_MATCHES:
                matches = matches[:MAX_GLOB_MATCHES]
                message += f" Only the first {MAX_GLOB_MATCHES} matches are returned."

            return _ToolOk(
                output="\n".join(str(p.relative_to(dir_path)) for p in matches),
                message=message,
            )
        except Exception as e:
            _logger.warning("Glob failed: pattern={pattern}: {error}", pattern=params.pattern, error=e)
            return _ToolError(
                message=f"Failed to search for pattern {params.pattern}. Error: {e}",
                brief="Glob failed",
            )

