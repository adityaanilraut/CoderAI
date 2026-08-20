"""First-class glob and grep tools backed by bundled ripgrep.

Port of dsh-tool-fs-search: spawn packaged `rg` with a plain argv vector (no shell),
cap inline results, and spill the complete formatted page when over cap. A Python
fallback is used when the binary is unavailable so hosts do not require `rg`.
"""

from __future__ import annotations

import fnmatch
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

from coderai.core.spill import SpillRef, try_save_text
from coderai.core.tools.types import ToolExecutionContext, ToolResult, as_str

GLOB_MAX_RESULTS = 100
GREP_MAX_MATCHES = 250
GREP_MAX_LINE_BYTES = 2000
RAW_OUTPUT_MAX_BYTES = 20_000_000
SEARCH_TIMEOUT_MS = 30_000
SEARCH_STDERR_MAX_BYTES = 64 * 1024
GLOB_VCS_EXCLUDES = (".git", ".svn", ".hg", ".bzr", ".jj", ".sl")

GLOB_DESCRIPTION = (
    "Find files whose paths match a glob pattern. Returns matching file paths — never directories — "
    "including hidden and ignored files (VCS metadata directories are excluded). "
    f"Up to {GLOB_MAX_RESULTS} paths come back in modification-time order; a larger result instead returns "
    f"{GLOB_MAX_RESULTS} paths sampled across top-level entries, says so, and reports where the complete "
    "sorted list was saved. This tool does not enumerate directory entries."
)
GREP_DESCRIPTION = (
    "Search file contents with a ripgrep regular expression. Returns matching lines with line numbers, grouped by file. "
    f"Returns the first {GREP_MAX_MATCHES} matches inline; a capped result reports where the complete match list was saved. "
    "Use read on a matched file for surrounding context."
)


class SearchError(Exception):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class GrepMatch:
    path: str
    line_number: int
    line: str


@dataclass
class RipgrepRun:
    stdout: str
    no_matches: bool
    workdir: str


def _vendor_rg_candidates() -> list[pathlib.Path]:
    vendor = pathlib.Path(__file__).resolve().parent.parent / "vendor"
    machine = (os.uname().machine if hasattr(os, "uname") else "").lower()
    plat = sys.platform
    names = ["rg", f"rg-{plat}", f"rg-{plat}-{machine}"]
    if plat == "darwin":
        names.append("rg-darwin-arm64" if "arm" in machine else "rg-darwin-x64")
    elif plat.startswith("linux"):
        names.append("rg-linux-arm64" if "arm" in machine or "aarch" in machine else "rg-linux-x64")
    elif plat == "win32":
        names.extend(["rg.exe", "rg-win32-x64.exe"])
    seen: list[pathlib.Path] = []
    for name in names:
        path = vendor / name
        if path not in seen:
            seen.append(path)
    return seen


def resolve_rg_path() -> str | None:
    """Resolve packaged rg, then CODERAI_RG_PATH, then PATH. None if unavailable."""
    env = os.environ.get("CODERAI_RG_PATH", "").strip()
    if env and os.path.isfile(env) and os.access(env, os.X_OK):
        return env
    for candidate in _vendor_rg_candidates():
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    found = shutil.which("rg")
    if found and os.access(found, os.X_OK):
        return found
    return None


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


def parse_grep_args(args: dict[str, Any]) -> dict[str, str]:
    pattern = as_str(args.get("pattern"))
    if pattern == "":
        raise ValueError("pattern must be a non-empty string")
    result: dict[str, str] = {"pattern": pattern}
    path = args.get("path")
    if path is not None:
        path_s = as_str(path)
        if not path_s.strip():
            raise ValueError("path must be a non-empty string when given")
        result["path"] = path_s
    include = args.get("include")
    if include is not None:
        include_s = as_str(include)
        _validate_include(include_s)
        result["include"] = include_s
    return result


def _validate_include(include: str) -> None:
    if not include.strip():
        raise ValueError("include must be a non-empty glob when given")
    if include.startswith("!"):
        raise ValueError(
            'include must be a positive glob filter; negated patterns ("!…") are not supported'
        )
    brace_depth = 0
    for char in include:
        if char == "{":
            brace_depth += 1
        elif char == "}":
            brace_depth = max(0, brace_depth - 1)
        elif char == "," and brace_depth == 0:
            raise ValueError(
                "include must be one glob, not a comma-separated list (use {a,b} alternation instead)"
            )


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


def build_grep_command(
    pattern: str, path: str | None = None, include: str | None = None
) -> list[str]:
    parts = ["--json", f"--regexp={pattern}"]
    if include is not None:
        parts.append(f"--glob={include}")
    if path is not None:
        parts.extend(["--", path])
    return parts


def to_workdir_relative(path: str, workdir: str) -> str:
    if not os.path.isabs(path):
        return path
    try:
        rel = os.path.relpath(path, workdir)
    except ValueError:
        return path
    if rel == ".":
        return "."
    if rel == ".." or rel.startswith(".." + os.sep):
        return path
    return rel.replace("\\", "/")


def preview_line(line: str, max_bytes: int = GREP_MAX_LINE_BYTES) -> str:
    encoded = line.encode("utf-8")
    if len(encoded) <= max_bytes:
        return line
    cut = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return f"{cut} (line truncated)"


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


def format_grep_matches(matches: list[GrepMatch]) -> str:
    by_file: dict[str, list[GrepMatch]] = {}
    for match in matches:
        by_file.setdefault(match.path, []).append(match)
    sections = []
    for path, group in by_file.items():
        rows = "\n".join(f"Line {m.line_number}: {m.line}" for m in group)
        sections.append(f"{path}\n{rows}")
    return "\n\n".join(sections)


def format_grep_output(
    matches: list[GrepMatch],
    *,
    seen: int,
    truncated: bool,
    spill_ref: SpillRef | None = None,
) -> str:
    if seen == 0:
        return "No matches found"
    noun = "match" if seen == 1 else "matches"
    header = f"Found {len(matches)} of {seen} matches" if truncated else f"Found {seen} {noun}"
    body = format_grep_matches(matches)
    if not truncated:
        return f"{header}\n\n{body}"
    if spill_ref is not None:
        recovery = f"Full grep result stored at: {spill_ref.locator}. {spill_ref.retrieval_hint}"
    else:
        recovery = (
            "The complete result could not be saved; narrow pattern, path, or include to see more."
        )
    return f"{header}\n\n{body}\n\n({recovery})"


def parse_grep_matches(stdout: str) -> list[GrepMatch]:
    matches: list[GrepMatch] = []
    for line in stdout.split("\n"):
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SearchError(
                f"grep received malformed ripgrep --json output (a line is not JSON): {exc}",
                "SEARCH_FAILED",
            ) from exc
        if not isinstance(parsed, dict):
            raise SearchError(
                "grep received malformed ripgrep --json output (a record is not an object)",
                "SEARCH_FAILED",
            )
        if parsed.get("type") != "match":
            continue
        data = parsed.get("data")
        if not isinstance(data, dict):
            raise SearchError(
                "grep received malformed ripgrep --json output (a match record has no data)",
                "SEARCH_FAILED",
            )
        path_obj = data.get("path")
        path_text = path_obj.get("text") if isinstance(path_obj, dict) else None
        if not isinstance(path_text, str):
            raise SearchError(
                "grep received malformed ripgrep --json output (a match record has no path text)",
                "SEARCH_FAILED",
            )
        line_number = data.get("line_number")
        if not isinstance(line_number, int):
            raise SearchError(
                "grep received malformed ripgrep --json output (a match record has no line number)",
                "SEARCH_FAILED",
            )
        lines = data.get("lines")
        if not isinstance(lines, dict):
            raise SearchError(
                "grep received malformed ripgrep --json output (a match record has no line content)",
                "SEARCH_FAILED",
            )
        if isinstance(lines.get("text"), str):
            text = lines["text"].replace("\r\n", "\n").removesuffix("\n").removesuffix("\r")
        elif isinstance(lines.get("bytes"), str):
            text = "(line is not valid UTF-8)"
        else:
            raise SearchError(
                "grep received malformed ripgrep --json output (a match record has neither line text nor bytes)",
                "SEARCH_FAILED",
            )
        matches.append(GrepMatch(path=path_text, line_number=line_number, line=text))
    return matches


def _rg_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("RIPGREP_CONFIG_PATH", None)
    return env


def run_ripgrep(
    argv: list[str],
    workdir: str,
    *,
    timeout_ms: int = SEARCH_TIMEOUT_MS,
    raw_output_max_bytes: int = RAW_OUTPUT_MAX_BYTES,
    tool_name: str = "search",
) -> RipgrepRun:
    rg = resolve_rg_path()
    if not rg:
        raise SearchError(
            f"{tool_name} could not start its search command (ripgrep binary not found)",
            "SEARCH_FAILED",
        )
    try:
        proc = subprocess.run(
            [rg, "--no-config", *argv],
            cwd=workdir,
            capture_output=True,
            timeout=max(0.1, timeout_ms / 1000.0),
            env=_rg_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise SearchError(
            f"{tool_name} was aborted before completion (tool timeout or caller cancellation)",
            "SEARCH_ABORTED",
        ) from exc
    except OSError as exc:
        raise SearchError(
            f"{tool_name} could not start its search command (ripgrep launch failed)",
            "SEARCH_FAILED",
        ) from exc

    stdout = (
        proc.stdout.decode("utf-8", errors="replace")
        if isinstance(proc.stdout, bytes)
        else (proc.stdout or "")
    )
    stderr = (
        proc.stderr.decode("utf-8", errors="replace")
        if isinstance(proc.stderr, bytes)
        else (proc.stderr or "")
    )
    if len(stderr.encode("utf-8")) > SEARCH_STDERR_MAX_BYTES:
        stderr = stderr.encode("utf-8")[-SEARCH_STDERR_MAX_BYTES:].decode("utf-8", errors="ignore")
        stderr = f"{stderr} [stderr truncated]"
    raw_bytes = len(stdout.encode("utf-8"))
    if raw_bytes > raw_output_max_bytes:
        raise SearchError(
            f"{tool_name} produced {raw_bytes} bytes of raw output, over the {raw_output_max_bytes}-byte cap; "
            "narrow pattern, path, or include and retry",
            "SEARCH_RAW_OUTPUT_OVERFLOW",
        )
    if proc.returncode not in (0, 1):
        if re.search(r"regex parse error|error parsing glob", stderr, re.I):
            raise SearchError(
                f"{tool_name} pattern rejected by ripgrep: {stderr.strip()}",
                "SEARCH_INVALID_PATTERN",
            )
        extra = f": {stderr.strip()}" if stderr.strip() else ""
        raise SearchError(
            f"{tool_name} search failed (exit {proc.returncode}){extra}", "SEARCH_FAILED"
        )
    return RipgrepRun(stdout=stdout, no_matches=proc.returncode == 1, workdir=workdir)


def _expand_braces(pattern: str) -> list[str]:
    match = re.search(r"\{([^{}]+)\}", pattern)
    if not match:
        return [pattern]
    inner = match.group(1)
    options = inner.split(",")
    prefix = pattern[: match.start()]
    suffix = pattern[match.end() :]
    expanded: list[str] = []
    for option in options:
        expanded.extend(_expand_braces(prefix + option + suffix))
    return expanded or [pattern]


def _matches_glob(rel_posix: str, pattern: str) -> bool:
    name = rel_posix.rsplit("/", 1)[-1]
    for alt in _expand_braces(pattern):
        alt = alt.replace("\\", "/")
        if "/" not in alt:
            if fnmatch.fnmatch(name, alt):
                return True
            continue
        if fnmatch.fnmatch(rel_posix, alt) or fnmatch.fnmatch(name, alt):
            return True
        # `**/*.ext` style
        if alt.startswith("**/") and fnmatch.fnmatch(rel_posix, alt[3:]):
            return True
        if fnmatch.fnmatch(rel_posix, alt.replace("**/", "")):
            return True
    return False


def _iter_workspace_files(root: pathlib.Path, deadline: float) -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    exclude = set(GLOB_VCS_EXCLUDES)
    for dirpath, dirnames, filenames in os.walk(root):
        if time.time() > deadline:
            break
        dirnames[:] = [d for d in dirnames if d not in exclude]
        base = pathlib.Path(dirpath)
        for name in filenames:
            path = base / name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
            except OSError:
                continue
            files.append(path)
    return files


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


def _python_grep(
    pattern: str,
    workdir: str,
    search_path: str | None,
    include: str | None,
    timeout_ms: int,
) -> list[GrepMatch]:
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise SearchError(f"grep pattern rejected: {exc}", "SEARCH_INVALID_PATTERN") from exc
    target = pathlib.Path(search_path or workdir)
    if not target.is_absolute():
        target = pathlib.Path(workdir) / target
    target = target.resolve()
    if not target.exists():
        raise SearchError(f"grep search failed: path not found: {target}", "SEARCH_FAILED")
    files: list[pathlib.Path]
    if target.is_file():
        files = [target]
    else:
        deadline = time.time() + max(0.1, timeout_ms / 1000.0)
        files = _iter_workspace_files(target, deadline)
    matches: list[GrepMatch] = []
    deadline = time.time() + max(0.1, timeout_ms / 1000.0)
    for path in files:
        if time.time() > deadline:
            break
        rel = to_workdir_relative(str(path), workdir).replace("\\", "/")
        if include is not None and not _matches_glob(rel, include):
            continue
        try:
            info = path.stat()
            if info.st_size > 2 * 1024 * 1024:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "\0" in text[:4096]:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                matches.append(GrepMatch(path=rel, line_number=i, line=line))
    return matches


def _prefer_python_backend() -> bool:
    return os.environ.get("CODERAI_SEARCH_BACKEND", "").strip().lower() in {"python", "fallback"}


def _session_workdir(context: ToolExecutionContext | Any) -> str:
    project_root = getattr(context, "project_root", None) or os.getcwd()
    return str(pathlib.Path(project_root).resolve())


def _session_id(context: ToolExecutionContext | Any) -> str:
    return str(getattr(context, "session_id", "") or "")


def _search_error_result(name: str, err: SearchError) -> ToolResult:
    return ToolResult(
        ok=False,
        name=name,
        error=f"Error: {err.message}",
        metadata={"name": "SearchError", "code": err.code},
    )


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


def handle_grep_tool(args: dict[str, Any], context: ToolExecutionContext | Any) -> ToolResult:
    try:
        parsed = parse_grep_args(args)
    except ValueError as exc:
        return ToolResult(ok=False, name="grep", error=str(exc))
    workdir = _session_workdir(context)
    pattern = parsed["pattern"]
    path = parsed.get("path")
    include = parsed.get("include")
    try:
        if _prefer_python_backend() or resolve_rg_path() is None:
            matches = _python_grep(pattern, workdir, path, include, SEARCH_TIMEOUT_MS)
        else:
            run = run_ripgrep(build_grep_command(pattern, path, include), workdir, tool_name="grep")
            if run.no_matches:
                matches = []
            else:
                matches = [
                    GrepMatch(
                        path=to_workdir_relative(raw.path, run.workdir),
                        line_number=raw.line_number,
                        line=raw.line,
                    )
                    for raw in parse_grep_matches(run.stdout)
                ]
    except SearchError as err:
        return _search_error_result("grep", err)

    previewed = [GrepMatch(m.path, m.line_number, preview_line(m.line)) for m in matches]
    spill_ref = None
    truncated = len(previewed) > GREP_MAX_MATCHES
    if truncated:
        spill_body = f"Found {len(previewed)} matches\n\n{format_grep_matches(previewed)}"
        spill_ref = try_save_text(
            session_id=_session_id(context),
            suggested_name="grep-results.txt",
            content=spill_body,
        )
    inline = previewed[:GREP_MAX_MATCHES]
    output = format_grep_output(
        inline, seen=len(previewed), truncated=truncated, spill_ref=spill_ref
    )
    return ToolResult(
        ok=True,
        name="grep",
        output=output,
        metadata={
            "matches": [
                {"path": m.path, "lineNumber": m.line_number, "line": m.line} for m in inline
            ],
            "count": len(previewed),
            "spill": spill_ref.to_dict() if spill_ref else None,
        },
    )


handle_glob = handle_glob_tool
handle_grep = handle_grep_tool
