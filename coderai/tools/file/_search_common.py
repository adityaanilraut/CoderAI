# Shared low-level plumbing for the file discovery tools.
"""Shared ripgrep/workspace search plumbing (`glob` + `grep`).

Single home for the validated PATH/explicit ripgrep resolver, the subprocess runner, the
workspace-relative path helpers, and the Python-fallback file walker, so the
per-tool modules (`glob.py`, `grep.py`) stay single-responsibility instead of
sharing one 700-line file.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from collections.abc import Iterator
from coderai.utils.bounded_process import bounded_run, OutputLimitError
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


def resolve_rg_path() -> str | None:
    """Select a launchable explicit/PATH rg; universal builds use Python otherwise.

    Native assets in older/source installs are deliberately ignored: the generic
    asset has no platform contract. A successful version probe also rejects a
    PATH executable for a different OS/architecture before search dispatch.
    """
    candidates = [os.environ.get("CODERAI_RG_PATH", "").strip(), shutil.which("rg")]
    for candidate in candidates:
        if not candidate or not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
            continue
        try:
            probe = subprocess.run(
                [candidate, "--version"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=2,
                env=_rg_env(),
            )
            if probe.returncode == 0 and probe.stdout.startswith(b"ripgrep "):
                return candidate
        except (OSError, subprocess.TimeoutExpired):
            continue
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
    cancellation_event: Any = None,
) -> RipgrepRun:
    deadline = time.monotonic() + max(0.1, timeout_ms / 1000.0)
    rg = resolve_rg_path()
    if time.monotonic() >= deadline:
        raise SearchError("Search deadline reached; narrow path and retry", "SEARCH_ABORTED")
    if not rg:
        raise SearchError(
            f"{tool_name} could not start its search command (ripgrep binary not found)",
            "SEARCH_UNAVAILABLE",
        )
    try:
        proc = bounded_run(
            [rg, "--no-config", *argv],
            cwd=workdir,
            timeout=max(0.001, deadline - time.monotonic()),
            stdout_limit=raw_output_max_bytes,
            env=_rg_env(),
            cancellation_event=cancellation_event,
        )
    except OutputLimitError as exc:
        raise SearchError(str(exc), "SEARCH_RAW_OUTPUT_OVERFLOW") from exc
    except subprocess.TimeoutExpired as exc:
        raise SearchError(
            f"{tool_name} was aborted before completion (tool timeout or caller cancellation)",
            "SEARCH_ABORTED",
        ) from exc
    except OSError as exc:
        raise SearchError(
            f"{tool_name} could not start its search command (ripgrep launch failed)",
            "SEARCH_UNAVAILABLE",
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
    from pathspec import GitIgnoreSpec

    # gitwildmatch handles separators and **; basename patterns match at any depth.
    if pattern.startswith("!"):
        return not _matches_glob(rel_posix, pattern[1:])
    return any(
        GitIgnoreSpec.from_lines([alt]).match_file(rel_posix) for alt in _expand_braces(pattern)
    )


def _iter_workspace_files(
    root: pathlib.Path, deadline: float, *, include_ignored: bool = True
) -> Iterator[pathlib.Path]:
    from pathspec import GitIgnoreSpec

    rules: list[tuple[pathlib.Path, Any]] = []
    if not include_ignored:
        ancestors = []
        for parent in (root, *root.parents):
            ancestors.append(parent)
            if (parent / ".git").exists():
                break
        for parent in reversed(ancestors):
            for ignore in (
                parent / ".git" / "info" / "exclude",
                parent / ".gitignore",
                parent / ".ignore",
                parent / ".rgignore",
            ):
                if ignore.is_file():
                    rules.append(
                        (parent, GitIgnoreSpec.from_lines(ignore.read_text().splitlines()))
                    )
        # Git's default global ignore file applies relative to the repository root.
        global_ignore = (
            pathlib.Path(os.environ.get("XDG_CONFIG_HOME", str(pathlib.Path.home() / ".config")))
            / "git"
            / "ignore"
        )
        git = shutil.which("git")
        if git:
            try:
                config = bounded_run(
                    [git, "config", "--path", "--get", "core.excludesFile"],
                    cwd=str(root),
                    timeout=min(1.0, max(0.001, deadline - time.monotonic())),
                    stdout_limit=4096,
                )
                if config.returncode == 0:
                    configured = config.stdout.decode().strip()
                    if configured:
                        global_ignore = pathlib.Path(configured).expanduser()
            except (OSError, subprocess.TimeoutExpired, OutputLimitError) as exc:
                raise SearchError(
                    f"Cannot resolve Git ignore policy: {exc}", "SEARCH_ABORTED"
                ) from exc
        if global_ignore.is_file():
            rules.insert(
                0,
                (
                    ancestors[-1] if ancestors else root,
                    GitIgnoreSpec.from_lines(global_ignore.read_text().splitlines()),
                ),
            )
    exclude = set(GLOB_VCS_EXCLUDES)
    for dirpath, dirnames, filenames in os.walk(root):
        if time.monotonic() >= deadline:
            raise SearchError(
                "Search deadline reached; narrow path or pattern and retry", "SEARCH_ABORTED"
            )
        base = pathlib.Path(dirpath)
        rules = [(directory, spec) for directory, spec in rules if base.is_relative_to(directory)]
        if not include_ignored:
            for ignore in (base / ".gitignore", base / ".ignore", base / ".rgignore"):
                if ignore.is_file():
                    try:
                        rules.append(
                            (base, GitIgnoreSpec.from_lines(ignore.read_text().splitlines()))
                        )
                    except OSError as exc:
                        raise SearchError(
                            f"Cannot read ignore rules: {exc}", "SEARCH_FAILED"
                        ) from exc

        def ignored(path: pathlib.Path, directory: bool = False) -> bool:
            result = False
            for parent, spec in rules:
                relative = path.relative_to(parent).as_posix() + ("/" if directory else "")
                match = spec.check_file(relative)
                if match.include is not None:
                    result = match.include
            return result

        dirnames[:] = [d for d in dirnames if d not in exclude and not ignored(base / d, True)]
        for name in filenames:
            if time.monotonic() >= deadline:
                raise SearchError(
                    "Search deadline reached; narrow path or pattern and retry", "SEARCH_ABORTED"
                )
            path = base / name
            try:
                if path.is_symlink() or not path.is_file() or ignored(path):
                    continue
            except OSError:
                continue
            yield path


def _prefer_python_backend() -> bool:
    return os.environ.get("CODERAI_SEARCH_BACKEND", "").strip().lower() in {"python", "fallback"}


def _session_workdir(context: ToolExecutionContext | Any) -> str:
    from coderai.tools.file.utils import get_effective_workdir

    return get_effective_workdir(context)


def _session_id(context: ToolExecutionContext | Any) -> str:
    return str(getattr(context, "session_id", "") or "")


def _search_error_result(name: str, err: SearchError) -> ToolResult:
    return ToolResult(
        ok=False,
        name=name,
        error=f"Error: {err.message}",
        metadata={
            "name": "SearchError",
            "code": err.code,
            "incomplete": err.code in {"SEARCH_ABORTED", "SEARCH_RAW_OUTPUT_OVERFLOW"},
            "retryable": err.code in {"SEARCH_ABORTED", "SEARCH_RAW_OUTPUT_OVERFLOW"},
        },
    )
