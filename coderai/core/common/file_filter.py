"""File filter with git-aware listing and ignored-names (Kimi parity)."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

_IGNORED_NAMES: frozenset[str] = frozenset(
    (
        ".DS_Store",
        ".bzr",
        ".git",
        ".hg",
        ".svn",
        ".build",
        ".cache",
        ".coverage",
        ".fleet",
        ".gradle",
        ".idea",
        ".ipynb_checkpoints",
        ".pnpm-store",
        ".pytest_cache",
        ".pub-cache",
        ".ruff_cache",
        ".swiftpm",
        ".tox",
        ".venv",
        ".vs",
        ".vscode",
        ".yarn",
        ".yarn-cache",
        ".next",
        ".nuxt",
        ".parcel-cache",
        ".svelte-kit",
        ".turbo",
        ".vercel",
        "node_modules",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "htmlcov",
        "pip-wheel-metadata",
        "venv",
        ".mvn",
        "out",
        "target",
        "bin",
        "cmake-build-debug",
        "cmake-build-release",
        "obj",
        "bazel-bin",
        "bazel-out",
        "bazel-testlogs",
        "buck-out",
        ".dart_tool",
        ".serverless",
        ".stack-work",
        ".terraform",
        ".terragrunt-cache",
        "DerivedData",
        "Pods",
        "deps",
        "tmp",
        "vendor",
    )
)

_IGNORED_PATTERNS: re.Pattern[str] = re.compile(
    r"|".join(
        (
            r".*_cache$",
            r".*-cache$",
            r".*\.egg-info$",
            r".*\.dist-info$",
            r".*\.py[co]$",
            r".*\.class$",
            r".*\.sw[po]$",
            r".*~$",
            r".*\.(?:tmp|bak)$",
        )
    ),
    re.IGNORECASE,
)

_GIT_LS_FILES_TIMEOUT = 5


def is_ignored(name: str) -> bool:
    if not name:
        return True
    if name in _IGNORED_NAMES:
        return True
    return bool(_IGNORED_PATTERNS.fullmatch(name))


def list_files_git(
    root: Path, scope: str | None = None, *, include_untracked: bool = True
) -> list[str] | None:
    if scope and ".." in scope.split("/"):
        return None
    scope_args = ["--", scope + "/"] if scope else []
    cmd = [
        "git",
        "-c",
        "core.quotepath=false",
        "ls-files",
        "-z",
        "--recurse-submodules",
        *scope_args,
    ]
    try:
        result = subprocess.run(
            cmd, cwd=root, capture_output=True, text=True, timeout=_GIT_LS_FILES_TIMEOUT
        )
        if result.returncode != 0:
            return None
    except Exception:
        return None
    paths = _parse_ls_files_output(result.stdout)
    if include_untracked:
        others_cmd = [
            "git",
            "-c",
            "core.quotepath=false",
            "ls-files",
            "-z",
            "--others",
            "--exclude-standard",
            *scope_args,
        ]
        try:
            others = subprocess.run(
                others_cmd, cwd=root, capture_output=True, text=True, timeout=_GIT_LS_FILES_TIMEOUT
            )
            if others.returncode == 0:
                tracked = set(paths)
                for p in _parse_ls_files_output(others.stdout):
                    if p not in tracked:
                        paths.append(p)
        except Exception:
            pass
    return paths


def _parse_ls_files_output(stdout: str) -> list[str]:
    paths: list[str] = []
    seen_dirs: set[str] = set()
    ignored_prefixes: set[str] = set()
    for entry in stdout.split("\0"):
        if not entry:
            continue
        parts = entry.split("/")
        skip = False
        for i, part in enumerate(parts):
            prefix = "/".join(parts[: i + 1]) + "/"
            if prefix in ignored_prefixes:
                skip = True
                break
            if is_ignored(part):
                ignored_prefixes.add(prefix)
                skip = True
                break
        if skip:
            continue
        for i in range(1, len(parts)):
            dir_path = "/".join(parts[:i]) + "/"
            if dir_path not in seen_dirs:
                seen_dirs.add(dir_path)
                paths.append(dir_path)
        paths.append(entry)
    return paths


def list_files_walk(root: Path, scope: str | None = None, *, limit: int = 1000) -> list[str]:
    resolved_root = root.resolve()
    walk_root = (root / scope).resolve() if scope else resolved_root
    try:
        if not walk_root.is_relative_to(resolved_root):
            return []
    except (OSError, ValueError):
        return []
    paths: list[str] = []
    try:
        for current_root, dirs, files in os.walk(walk_root):
            relative_root = Path(current_root).resolve().relative_to(resolved_root)
            dirs[:] = sorted(d for d in dirs if not is_ignored(d))
            if relative_root.parts and any(is_ignored(part) for part in relative_root.parts):
                dirs[:] = []
                continue
            if relative_root.parts:
                paths.append(relative_root.as_posix() + "/")
                if len(paths) >= limit:
                    break
            for file_name in sorted(files):
                if is_ignored(file_name):
                    continue
                relative = (relative_root / file_name).as_posix()
                if not relative:
                    continue
                paths.append(relative)
                if len(paths) >= limit:
                    break
            if len(paths) >= limit:
                break
    except OSError:
        pass
    return paths


def list_directory_filtered(directory: Path) -> list[dict[str, str | int]]:
    result: list[dict[str, str | int]] = []
    try:
        for subpath in directory.iterdir():
            if is_ignored(subpath.name):
                continue
            if subpath.is_dir():
                result.append({"name": subpath.name, "type": "directory"})
            else:
                try:
                    size = subpath.stat().st_size
                except OSError:
                    size = 0
                result.append({"name": subpath.name, "type": "file", "size": size})
    except OSError:
        pass
    result.sort(key=lambda x: (str(x["type"]), str(x["name"])))
    return result
