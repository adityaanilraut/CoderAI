"""Workspace file mention parser and context expansion (@file)."""

from __future__ import annotations

import os
import pathlib
import re

FILE_MENTION_PATTERN = re.compile(r"@([A-Za-z0-9_\-./\\]+(?::L?\d+(?:-\d+)?)?)")
IGNORED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
}


def _find_matching_file(project_root: str, file_ref: str) -> pathlib.Path | None:
    """Find matching file by exact relative path, absolute path, or filename search."""
    root = pathlib.Path(project_root).resolve()

    # 1. Exact path relative to root
    exact = (root / file_ref).resolve()
    if exact.is_file():
        return exact

    # 2. Absolute path if provided
    if pathlib.Path(file_ref).is_absolute() and pathlib.Path(file_ref).is_file():
        return pathlib.Path(file_ref).resolve()

    # 3. Filename search in workspace tree
    target_name = pathlib.Path(file_ref).name.lower()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        for f in filenames:
            if f.lower() == target_name:
                return (pathlib.Path(dirpath) / f).resolve()

    return None


def _parse_line_range(spec: str) -> tuple[str, int | None, int | None]:
    """Parse '@file.py:10-20' or '@file.py:L10-L20' or '@file.py:15'."""
    if ":" not in spec:
        return spec, None, None
    path_part, range_part = spec.split(":", 1)
    range_part = range_part.replace("L", "").replace("l", "").strip()
    if "-" in range_part:
        start_str, end_str = range_part.split("-", 1)
        try:
            return path_part, int(start_str), int(end_str)
        except ValueError:
            return path_part, None, None
    try:
        single = int(range_part)
        return path_part, single, single
    except ValueError:
        return path_part, None, None


def read_file_mention_snippet(
    file_path: pathlib.Path, start_line: int | None, end_line: int | None
) -> str:
    """Read full file or slice of file content."""
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"[Error reading {file_path.name}: {e}]"

    lines = text.splitlines()
    if start_line is not None and end_line is not None:
        start_idx = max(1, start_line)
        end_idx = min(len(lines), end_line)
        sliced = lines[start_idx - 1 : end_idx]
        return "\n".join(f"{idx}: {line}" for idx, line in enumerate(sliced, start=start_idx))

    return text


def expand_file_mentions(prompt: str, project_root: str) -> tuple[str, list[str]]:
    """Expand all @file mentions and @session references in the user prompt into embedded contexts."""
    from coderai.core.common.session_reference import resolve_session_references

    matches = FILE_MENTION_PATTERN.findall(prompt)
    attached_files: list[str] = []
    snippets: list[str] = []

    for match in matches:
        if match.startswith("session:"):
            continue
        file_ref, start_line, end_line = _parse_line_range(match)
        target_path = _find_matching_file(project_root, file_ref)
        if target_path and target_path.is_file():
            rel_path = str(target_path.relative_to(pathlib.Path(project_root).resolve()))
            attached_files.append(rel_path)
            content = read_file_mention_snippet(target_path, start_line, end_line)
            range_info = f" (lines {start_line}-{end_line})" if start_line and end_line else ""
            snippet_block = f"--- Attached Context: {rel_path}{range_info} ---\n{content}\n--- End Attached Context ---"
            snippets.append(snippet_block)

    _, session_refs, session_context = resolve_session_references(project_root, prompt)
    if session_context:
        snippets.append(session_context)
        for sref in session_refs:
            if sref.get("resolved"):
                attached_files.append(f"session:{sref['sessionId']}")

    if not snippets:
        return prompt, []

    expanded_prompt = prompt + "\n\n" + "\n\n".join(snippets)
    return expanded_prompt, attached_files


def suggest_workspace_files(query: str, project_root: str, limit: int = 15) -> list[str]:
    """Search and fuzzy rank workspace files for autocompletion."""
    root = pathlib.Path(project_root).resolve()
    query_clean = query.lstrip("@")
    all_files: list[str] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith(".")]
        for f in filenames:
            if f.startswith(".") and not query_clean.startswith("."):
                continue
            full = pathlib.Path(dirpath) / f
            rel = str(full.relative_to(root))
            all_files.append(rel)

    # Prioritize shallow files in workspace hierarchy
    all_files.sort(key=lambda p: (p.count(os.sep), len(p)))

    from coderai.cli.fuzzy import fuzzy_filter

    return fuzzy_filter(query_clean, all_files, limit=limit)
