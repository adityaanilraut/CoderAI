# Shared low-level plumbing for the file discovery tools.
"""Shared ripgrep/workspace search plumbing (`glob` + `grep`).

Single home for the vendored-ripgrep resolver, the subprocess runner, the
workspace-relative path helpers, and the Python-fallback file walker, so the
per-tool modules (`glob.py`, `grep.py`) stay single-responsibility instead of
sharing one 700-line file.
"""

from __future__ import annotations

import fnmatch
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

from coderai.tools.legacy.types import ToolExecutionContext, ToolResult

SEARCH_TIMEOUT_MS = 30_000
SEARCH_STDERR_MAX_BYTES = 64 * 1024
RAW_OUTPUT_MAX_BYTES = 20_000_000
GLOB_VCS_EXCLUDES = (".git", ".svn", ".hg", ".bzr", ".jj", ".sl")


class SearchError(Exception):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class RipgrepRun:
    stdout: str
    no_matches: bool
    workdir: str


def _vendor_rg_candidates() -> list[pathlib.Path]:
    # This module lives at coderai/tools/file/; the packaged binary is at coderai/vendor/.
    vendor = pathlib.Path(__file__).resolve().parent.parent.parent / "vendor"
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
            # Never inherit stdin: with no path operand rg heuristically searches
            # stdin when it looks readable, silently returning no matches.
            stdin=subprocess.DEVNULL,
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
