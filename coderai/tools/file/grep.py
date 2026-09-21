"""First-class grep tool backed by bundled ripgrep.

Search surface (`output_mode`, context windows, `head_limit` /
`offset` pagination, sensitive-file filtering) on top of CoderAI's `--json`
ripgrep engine plus the Python-fallback backend.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import stat
import time
from dataclasses import dataclass
from typing import Any

from coderai.spill import SpillRef, try_save_text
from coderai.tools.file._search_common import (
    GLOB_VCS_EXCLUDES,
    SEARCH_TIMEOUT_MS,
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
from coderai.utils.sensitive import is_sensitive_file, sensitive_file_warning

GREP_MAX_MATCHES = 250
GREP_DEFAULT_HEAD_LIMIT = 250
GREP_MAX_LINE_BYTES = 2000
GREP_OUTPUT_MODES = ("content", "files_with_matches", "count_matches")

GREP_DESCRIPTION = (
    "Search file contents with a ripgrep regular expression. Returns matching lines with line numbers, grouped by file. "
    f"Returns the first {GREP_MAX_MATCHES} matches inline; a capped result reports where the complete match list was saved. "
    "Use read on a matched file for surrounding context."
)


@dataclass
class GrepMatch:
    path: str
    line_number: int
    line: str
    is_context: bool = False


def _parse_optional_int(args: dict[str, Any], name: str) -> int | None:
    value = args.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a non-negative integer")
    ivalue = int(value)
    if ivalue < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return ivalue


def _parse_bool(args: dict[str, Any], name: str, default: bool) -> bool:
    value = args.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


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


def parse_grep_args(args: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise ValueError("args must be an object")
    pattern = as_str(args.get("pattern"))
    if pattern == "":
        raise ValueError("pattern must be a non-empty string")
    result: dict[str, Any] = {"pattern": pattern}
    path = args.get("path")
    if path is not None:
        path_s = as_str(path)
        if not path_s.strip():
            raise ValueError("path must be a non-empty string when given")
        result["path"] = path_s
    include_raw = args.get("include")
    if include_raw is None:
        include_raw = args.get("glob")
    if include_raw is not None:
        include_s = as_str(include_raw)
        _validate_include(include_s)
        result["include"] = include_s
    output_mode = as_str(args.get("output_mode"), "content") or "content"
    if output_mode not in GREP_OUTPUT_MODES:
        raise ValueError(f"output_mode must be one of: {', '.join(GREP_OUTPUT_MODES)}")
    result["output_mode"] = output_mode
    for key in ("before_context", "after_context", "context"):
        value = _parse_optional_int(args, key)
        if value is not None:
            if output_mode != "content":
                raise ValueError(f"{key} requires output_mode to be 'content'")
            result[key] = value
    result["line_number"] = _parse_bool(args, "line_number", True)
    result["ignore_case"] = _parse_bool(args, "ignore_case", False)
    file_type = args.get("type")
    if file_type is not None:
        file_type_s = as_str(file_type)
        if not file_type_s.strip():
            raise ValueError("type must be a non-empty string when given")
        result["file_type"] = file_type_s
    head_limit = _parse_optional_int(args, "head_limit")
    result["head_limit"] = GREP_DEFAULT_HEAD_LIMIT if head_limit is None else head_limit
    result["offset"] = _parse_optional_int(args, "offset") or 0
    result["multiline"] = _parse_bool(args, "multiline", False)
    result["include_ignored"] = _parse_bool(args, "include_ignored", False)
    return result


def build_grep_command(
    pattern: str,
    path: str | None = None,
    include: str | None = None,
    *,
    ignore_case: bool = False,
    file_type: str | None = None,
    multiline: bool = False,
    include_ignored: bool = False,
    before_context: int | None = None,
    after_context: int | None = None,
    context: int | None = None,
) -> list[str]:
    parts = ["--json", f"--regexp={pattern}"]
    # Hidden files are always searched (matches the Python
    # fallback which walks dotfiles); sensitive files are filtered afterwards.
    parts.append("--hidden")
    if ignore_case:
        parts.append("--ignore-case")
    if multiline:
        parts.append("--multiline")
    if include_ignored:
        parts.append("--no-ignore")
    for name in GLOB_VCS_EXCLUDES:
        parts.append(f"--glob=!**/{name}")
        parts.append(f"--glob=!**/{name}/**")
    if before_context is not None:
        parts.extend(["--before-context", str(before_context)])
    if after_context is not None:
        parts.extend(["--after-context", str(after_context)])
    if context is not None:
        parts.extend(["--context", str(context)])
    if include is not None:
        parts.append(f"--glob={include}")
    if file_type is not None:
        parts.extend(["--type", file_type])
    if path is not None:
        parts.extend(["--", path])
    return parts


def preview_line(line: str, max_bytes: int = GREP_MAX_LINE_BYTES) -> str:
    encoded = line.encode("utf-8")
    if len(encoded) <= max_bytes:
        return line
    cut = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return f"{cut} (line truncated)"


def format_grep_matches(matches: list[GrepMatch], *, line_number: bool = True) -> str:
    by_file: dict[str, list[GrepMatch]] = {}
    for match in matches:
        by_file.setdefault(match.path, []).append(match)
    sections = []
    for path, group in by_file.items():
        rows = []
        for m in group:
            if m.is_context:
                rows.append(f"Line {m.line_number}- {m.line}" if line_number else f"  {m.line}")
            else:
                rows.append(f"Line {m.line_number}: {m.line}" if line_number else m.line)
        sections.append(f"{path}\n" + "\n".join(rows))
    return "\n\n".join(sections)


def format_grep_output(
    matches: list[GrepMatch],
    *,
    seen: int,
    truncated: bool,
    spill_ref: SpillRef | None = None,
    line_number: bool = True,
    offset: int = 0,
    notice: str = "",
) -> str:
    if seen == 0:
        base = "No matches found"
        return f"{base} ({notice})" if notice else base
    noun = "match" if seen == 1 else "matches"
    header = f"Found {len(matches)} of {seen} matches" if truncated else f"Found {seen} {noun}"
    if offset:
        header += f" (offset {offset})"
    body = format_grep_matches(matches, line_number=line_number)
    if not truncated:
        return f"{header}\n\n{body}" + (f"\n\n({notice})" if notice else "")
    if spill_ref is not None:
        recovery = f"Full grep result stored at: {spill_ref.locator}. {spill_ref.retrieval_hint}"
    else:
        recovery = (
            "The complete result could not be saved; narrow pattern, path, or include to see more."
        )
    out = f"{header}\n\n{body}\n\n({recovery})"
    return f"{out} ({notice})" if notice else out


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
        kind = parsed.get("type")
        if kind not in ("match", "context"):
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
        matches.append(
            GrepMatch(
                path=path_text,
                line_number=line_number,
                line=text,
                is_context=(kind == "context"),
            )
        )
    return matches


def _python_grep(
    pattern: str,
    workdir: str,
    search_path: str | None,
    include: str | None,
    timeout_ms: int,
    *,
    ignore_case: bool = False,
    before_context: int = 0,
    after_context: int = 0,
) -> list[GrepMatch]:
    try:
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
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
        lines = text.splitlines()
        hits = [bool(regex.search(line)) for line in lines]
        if not any(hits):
            continue
        wanted = [False] * len(lines)
        for i, hit in enumerate(hits):
            if hit:
                lo = max(0, i - before_context)
                hi = min(len(lines), i + after_context + 1)
                for j in range(lo, hi):
                    wanted[j] = True
        for i, want in enumerate(wanted):
            if want:
                matches.append(
                    GrepMatch(path=rel, line_number=i + 1, line=lines[i], is_context=not hits[i])
                )
    return matches


def _filter_sensitive(matches: list[GrepMatch]) -> tuple[list[GrepMatch], str]:
    """Drop matches from sensitive files. Returns (kept, notice)."""
    filtered: list[str] = []
    kept: list[GrepMatch] = []
    for match in matches:
        if is_sensitive_file(match.path):
            if match.path not in filtered:
                filtered.append(match.path)
        else:
            kept.append(match)
    return kept, sensitive_file_warning(filtered) if filtered else ""


def _mtime(workdir: str, rel: str) -> float:
    try:
        return os.path.getmtime(os.path.join(workdir, rel))
    except OSError:
        return 0.0


def _grep_files_result(
    matches: list[GrepMatch],
    workdir: str,
    offset: int,
    head_limit: int,
    notice: str,
) -> ToolResult:
    paths = sorted(
        {m.path for m in matches},
        key=lambda p: _mtime(workdir, p),
        reverse=True,
    )
    total = len(paths)
    page = paths[offset:]
    effective = head_limit
    truncated = effective > 0 and len(page) > effective
    inline = page[:effective] if effective else page
    if not inline:
        output = "No matches found"
    else:
        output = "\n".join(inline)
        if truncated or offset:
            show_to = offset + len(inline)
            output += (
                f"\n\n(Showing {offset + 1}–{show_to} of {total} files"
                + (", truncated" if truncated else "")
                + ".)"
            )
    if notice:
        output += f"\n\n({notice})"
    return ToolResult(
        ok=True,
        name="grep",
        output=output,
        metadata={
            "paths": inline,
            "count": total,
            "output_mode": "files_with_matches",
            "truncated": truncated,
        },
    )


def _grep_count_result(
    matches: list[GrepMatch],
    offset: int,
    head_limit: int,
    notice: str,
) -> ToolResult:
    counts: dict[str, int] = {}
    for match in matches:
        if not match.is_context:
            counts[match.path] = counts.get(match.path, 0) + 1
    lines = [f"{path}:{counts[path]}" for path in sorted(counts)]
    total_matches = sum(counts.values())
    total_files = len(counts)
    summary = f"Found {total_matches} total occurrences across {total_files} files."
    page = lines[offset:]
    effective = head_limit
    truncated = effective > 0 and len(page) > effective
    inline = page[:effective] if effective else page
    if not inline:
        output = f"No matches found. {summary}"
    else:
        output = "\n".join(inline)
        if truncated or offset:
            show_to = offset + len(inline)
            output += f"\n\n(Showing {offset + 1}–{show_to} of {len(lines)} files, truncated.)"
        output += f"\n\n({summary})"
    if notice:
        output += f"\n\n({notice})"
    return ToolResult(
        ok=True,
        name="grep",
        output=output,
        metadata={
            "counts": counts,
            "count": total_matches,
            "output_mode": "count_matches",
            "truncated": truncated,
        },
    )


def _grep_content_result(
    matches: list[GrepMatch],
    context: ToolExecutionContext | Any,
    offset: int,
    head_limit: int,
    line_number: bool,
    notice: str,
) -> ToolResult:
    previewed = [
        GrepMatch(m.path, m.line_number, preview_line(m.line), m.is_context) for m in matches
    ]
    # Header counts real matches only; context rows are display companions.
    total = sum(1 for m in previewed if not m.is_context)
    page = previewed[offset:]
    effective = head_limit
    truncated = effective > 0 and len(page) > effective
    inline = page[:effective] if effective else page
    spill_ref = None
    if truncated:
        spill_body = f"Found {total} matches\n\n{format_grep_matches(previewed)}"
        spill_ref = try_save_text(
            session_id=_session_id(context),
            suggested_name="grep-results.txt",
            content=spill_body,
        )
    output = format_grep_output(
        inline,
        seen=total,
        truncated=truncated,
        spill_ref=spill_ref,
        line_number=line_number,
        offset=offset,
        notice=notice,
    )
    return ToolResult(
        ok=True,
        name="grep",
        output=output,
        metadata={
            "matches": [
                {"path": m.path, "lineNumber": m.line_number, "line": m.line} for m in inline
            ],
            "count": total,
            "output_mode": "content",
            "truncated": truncated,
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
    output_mode = parsed["output_mode"]
    before_context = parsed.get("before_context", 0) or 0
    after_context = parsed.get("after_context", 0) or 0
    context_lines = parsed.get("context")
    if context_lines is not None:
        before_context = after_context = context_lines
    try:
        if _prefer_python_backend() or resolve_rg_path() is None:
            matches = _python_grep(
                pattern,
                workdir,
                path,
                include,
                SEARCH_TIMEOUT_MS,
                ignore_case=parsed["ignore_case"],
                before_context=before_context,
                after_context=after_context,
            )
        else:
            run = run_ripgrep(
                build_grep_command(
                    pattern,
                    path,
                    include,
                    ignore_case=parsed["ignore_case"],
                    file_type=parsed.get("file_type"),
                    multiline=parsed["multiline"],
                    include_ignored=parsed["include_ignored"],
                    before_context=parsed.get("before_context"),
                    after_context=parsed.get("after_context"),
                    context=parsed.get("context"),
                ),
                workdir,
                tool_name="grep",
            )
            if run.no_matches:
                matches = []
            else:
                matches = [
                    GrepMatch(
                        path=to_workdir_relative(raw.path, run.workdir),
                        line_number=raw.line_number,
                        line=raw.line,
                        is_context=raw.is_context,
                    )
                    for raw in parse_grep_matches(run.stdout)
                ]
    except SearchError as err:
        return _search_error_result("grep", err)

    matches, notice = _filter_sensitive(matches)
    offset = parsed["offset"]
    head_limit = parsed["head_limit"]
    if output_mode == "files_with_matches":
        return _grep_files_result(matches, workdir, offset, head_limit, notice)
    if output_mode == "count_matches":
        return _grep_count_result(matches, offset, head_limit, notice)
    return _grep_content_result(matches, context, offset, head_limit, parsed["line_number"], notice)


handle_grep = handle_grep_tool


def grep_tool_definition() -> ToolDefinition:
    """Per-tool registry definition for `grep` (keeps `registry.py` thin)."""
    return define_tool(
        name="grep",
        description=GREP_DESCRIPTION,
        parameters={
            "pattern": {
                "type": "string",
                "description": "Regular expression to search for (ripgrep syntax).",
            },
            "path": {
                "type": "string",
                "description": "File or directory to search. Defaults to the session workspace; a relative path resolves against it.",
            },
            "include": {
                "type": "string",
                "description": 'One glob filter for which files to search (e.g. "*.ts", "*.{js,jsx}"). Not a list; negation is not supported.',
            },
            "glob": {
                "type": "string",
                "description": "Alias of include: glob pattern to filter files (e.g. `*.js`, `*.{ts,tsx}`).",
            },
            "output_mode": {
                "type": "string",
                "description": "`content`: matching lines (default); `files_with_matches`: file paths only; `count_matches`: per-file match counts.",
                "enum": ["content", "files_with_matches", "count_matches"],
            },
            "before_context": {
                "type": "integer",
                "description": "Lines to show before each match. Requires output_mode `content`.",
            },
            "after_context": {
                "type": "integer",
                "description": "Lines to show after each match. Requires output_mode `content`.",
            },
            "context": {
                "type": "integer",
                "description": "Lines to show before and after each match. Requires output_mode `content`.",
            },
            "line_number": {
                "type": "boolean",
                "description": "Show line numbers in content mode. Defaults to true.",
            },
            "ignore_case": {
                "type": "boolean",
                "description": "Case-insensitive search. Defaults to false.",
            },
            "type": {
                "type": "string",
                "description": "Ripgrep file type to search (e.g. py, js, ts, rust). More efficient than include for standard types.",
            },
            "head_limit": {
                "type": "integer",
                "description": "Limit output to the first N entries (default 250, 0 for unlimited).",
            },
            "offset": {
                "type": "integer",
                "description": "Skip the first N entries before applying head_limit. Defaults to 0.",
            },
            "multiline": {
                "type": "boolean",
                "description": "Multiline mode where patterns can span lines. Defaults to false.",
            },
            "include_ignored": {
                "type": "boolean",
                "description": "Search files ignored by .gitignore (sensitive files stay filtered). Defaults to false.",
            },
        },
        required=["pattern"],
        handler=handle_grep_tool,
        category="filesystem",
        is_mutating=False,
        is_concurrency_safe=True,
    )


# --- CallableTool2 Implementation ---

from pathlib import Path as _Path
from kosong.tooling import (
    CallableTool2 as _CallableTool2,
    ToolError as _ToolError,
    ToolOk as _ToolOk,
    ToolReturnValue as _ToolReturnValue,
)
from pydantic import BaseModel as _BaseModel, Field as _Field

from coderai.soul.agent import Runtime as _Runtime
from coderai.tools.utils import load_desc as _load_desc

_BASE_GREP_DESCRIPTION = _load_desc(_Path(__file__).parent / "grep.md")


class GrepParams(_BaseModel):
    pattern: str = _Field(
        description="The regular expression pattern to search for in file contents"
    )
    path: str = _Field(
        description="File or directory to search in. Defaults to current working directory.",
        default=".",
    )
    glob: str | None = _Field(
        description="Glob pattern to filter files (e.g. `*.js`, `*.{ts,tsx}`). No filter by default.",
        default=None,
    )
    output_mode: str = _Field(
        description="`content`, `files_with_matches`, or `count_matches`. Defaults to `files_with_matches`.",
        default="files_with_matches",
    )
    case_insensitive: bool = _Field(default=False)
    head_limit: int = _Field(default=250)
    offset: int = _Field(default=0)
    multiline: bool = _Field(default=False)


class Grep(_CallableTool2[GrepParams]):
    name: str = "Grep"
    description: str = _BASE_GREP_DESCRIPTION
    params: type[GrepParams] = GrepParams

    def __init__(self, runtime: _Runtime) -> None:
        super().__init__()
        self._runtime = runtime

    async def __call__(self, params: GrepParams) -> _ToolReturnValue:
        args = {
            "pattern": params.pattern,
            "path": params.path,
            "include": params.glob,
            "output_mode": params.output_mode,
            "case_insensitive": params.case_insensitive,
            "head_limit": params.head_limit,
            "offset": params.offset,
            "multiline": params.multiline,
        }
        res = handle_grep_tool(args, None)
        if not res.ok:
            return _ToolError(message=res.error or "Grep failed", brief="Grep error")
        return _ToolOk(
            output=res.output or "", message=f"Grep found matches for `{params.pattern}`."
        )
