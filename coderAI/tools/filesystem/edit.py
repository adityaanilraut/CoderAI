"""In-place editing tools: search/replace and unified-diff patching."""

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from coderAI.types.tool_error_codes import ToolErrorCode
from coderAI.system.locks import get_lock_manager
from coderAI.tools.base import Tool, ToolPreview
from coderAI.tools.undo import get_backup_store

from coderAI.tools.filesystem._guards import (
    _atomic_write_file,
    _emit_diff,
    _enforce_project_scope,
    _is_path_protected,
    _reject_symlink_leaf,
    _safe_open_no_symlink,
    _get_max_file_size,
)

logger = logging.getLogger(__name__)


class EditChunk(BaseModel):
    search: str = Field(..., min_length=1, description="Exact text to search for")
    replace: str = Field(..., description="Text to replace it with")
    expected_count: int = Field(1, description="Expected number of occurrences to replace")


class SearchReplaceParams(BaseModel):
    path: str = Field(..., description="Path to the file")
    search: str = Field("", description="Text to search for")
    replace: str = Field("", description="Text to replace with")
    replace_all: bool = Field(False, description="Replace all occurrences (default: first only)")
    edits: Optional[List[EditChunk]] = Field(
        None, description="Apply multiple search/replace edits in a single atomic operation"
    )


class SearchReplaceTool(Tool):
    """Tool for search and replace in files — single or batch edit mode.

    Supply ``search`` / ``replace`` for a single edit, or ``edits`` for
    multiple edits applied atomically in one write.
    """

    name = "search_replace"
    description = (
        "Search for text in a file and replace it. "
        "Accepts an optional 'edits' list for batch search/replace operations."
    )
    parameters_model = SearchReplaceParams
    requires_confirmation = True
    category = "filesystem"
    batch_serialize_by_path = True

    @staticmethod
    def _apply(content: str, search: str, replace: str, replace_all: bool) -> str:
        """The single search/replace semantics shared by execute() and preview()."""
        if replace_all:
            return content.replace(search, replace)
        return content.replace(search, replace, 1)

    def preview(self, arguments: dict[str, Any], original: Optional[str]) -> Optional[ToolPreview]:
        """Resulting file content after the replacement(s)."""
        if original is None:
            return None
        edits = arguments.get("edits") or []
        if edits and isinstance(edits, list) and len(edits) > 0:
            new_content = original
            for edit in edits:
                if not isinstance(edit, dict):
                    return None
                new_content = new_content.replace(
                    str(edit.get("search", "")), str(edit.get("replace", ""))
                )
            return ToolPreview(new_content=new_content)
        search = str(arguments.get("search", "") or "")
        if not search:
            return None
        replace = str(arguments.get("replace", "") or "")
        replace_all = bool(arguments.get("replace_all", False))
        return ToolPreview(new_content=self._apply(original, search, replace, replace_all))

    async def execute(  # type: ignore[override]
        self,
        path: str,
        search: str = "",
        replace: str = "",
        replace_all: bool = False,
        edits: Optional[List[Dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """Search and replace in file with protection. Supports batch edits via *edits*."""
        try:
            if not path or not str(path).strip():
                return {
                    "success": False,
                    "error": "path is required and must be a non-empty file path.",
                }

            path_obj = Path(path).expanduser()

            lock = await get_lock_manager().get_file_lock(str(path_obj))
            async with lock:
                if _is_path_protected(path_obj):
                    return {
                        "success": False,
                        "error": f"Cannot modify protected path: {path}",
                    }
                scope_err = _enforce_project_scope(path_obj, "search_replace")
                if scope_err:
                    return scope_err

                if path_obj.is_dir():
                    return {
                        "success": False,
                        "error": f"Path is a directory, not a file: {path}",
                    }

                if not path_obj.exists():
                    return {
                        "success": False,
                        "error": f"File not found: {path}",
                        "hint": "Check the path with list_directory or glob_search.",
                    }

                symlink_err = _reject_symlink_leaf(path_obj, "search_replace in")
                if symlink_err:
                    return symlink_err

                def _read() -> str:
                    with _safe_open_no_symlink(path_obj, "r") as f:
                        return f.read()

                try:
                    content = await asyncio.to_thread(_read)
                except OSError as e:
                    return {
                        "success": False,
                        "error": f"Could not open {path}: {e}",
                        "error_code": ToolErrorCode.SYMLINK
                        if "loop" in str(e).lower()
                        else ToolErrorCode.IO,
                    }

                # ---- Batch mode (edits list) ----
                if edits and isinstance(edits, list) and len(edits) > 0:
                    stat = path_obj.stat()
                    file_size = stat.st_size
                    max_file_size = _get_max_file_size()
                    if file_size > max_file_size:
                        return {
                            "success": False,
                            "error": f"File too large: {file_size:,} bytes (limit: {max_file_size:,} bytes).",
                            "error_code": ToolErrorCode.TOO_LARGE,
                            "hint": "Use single-edit or write_file for targeted modifications on large files.",
                        }

                    new_content = content
                    actual_counts: list[int] = []
                    count_mismatches: list[dict[str, Any]] = []
                    for i, edit in enumerate(edits):
                        s = edit["search"]
                        r = edit.get("replace", "")
                        expected_count = edit.get("expected_count", 1)

                        if not s:
                            return {
                                "success": False,
                                "error": f"Edit {i + 1} failed: search text must be non-empty.",
                                "hint": "Each edit needs a non-empty search string.",
                            }

                        actual_count = new_content.count(s)
                        actual_counts.append(actual_count)
                        if actual_count == 0:
                            return {
                                "success": False,
                                "error": f"Edit {i + 1} failed: expected to find search text, found 0 occurrences.",
                                "hint": "Check the file contents and make sure the search text exactly matches what's in the file.",
                            }
                        if expected_count == 1 and actual_count > 1:
                            logger.warning(
                                "search_replace: edit %d matches %d occurrences but expected_count=1",
                                i + 1,
                                actual_count,
                            )
                        if actual_count != expected_count:
                            count_mismatches.append(
                                {
                                    "edit_index": i,
                                    "expected_count": expected_count,
                                    "actual_count": actual_count,
                                }
                            )
                        new_content = new_content.replace(s, r)

                    await asyncio.to_thread(get_backup_store().backup_file, str(path_obj), "modify")
                    await asyncio.to_thread(_atomic_write_file, path_obj, new_content)
                    _emit_diff(path_obj, content, new_content)

                    return {
                        "success": True,
                        "path": str(path_obj),
                        "edits_applied": len(edits),
                        "actual_counts": actual_counts,
                        "count_mismatches": count_mismatches,
                    }

                # ---- Single-edit mode ----
                if search == "":
                    return {
                        "success": False,
                        "error": "search text must be non-empty.",
                    }

                if search not in content:
                    return {
                        "success": False,
                        "error": "Search text not found in file",
                        "hint": "Use grep to verify the exact text in the file.",
                    }

                get_backup_store().backup_file(str(path_obj), "modify")

                new_content = self._apply(content, search, replace, replace_all)
                count = content.count(search) if replace_all else 1

                try:
                    await asyncio.to_thread(_atomic_write_file, path_obj, new_content)
                except OSError as e:
                    return {
                        "success": False,
                        "error": f"Could not write {path}: {e}",
                        "error_code": ToolErrorCode.SYMLINK
                        if "loop" in str(e).lower()
                        else ToolErrorCode.IO,
                    }

                _emit_diff(path_obj, content, new_content)

                return {
                    "success": True,
                    "path": str(path_obj),
                    "replacements": count,
                }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


# --- Apply Diff Tool (F2) ---


class ApplyDiffParams(BaseModel):
    path: str = Field(..., description="Path to the file to patch")
    diff: str = Field(
        ...,
        description=(
            "Unified diff to apply. Lines starting with '-' are removed, "
            "'+' are added, ' ' (space) are context. Include @@ hunk headers."
        ),
    )


class ApplyDiffTool(Tool):
    """Tool for applying unified diffs to files."""

    name = "apply_diff"
    description = (
        "Apply a unified diff (patch) to a file. More precise than search_replace "
        "for multi-line edits. Creates a backup for undo support."
    )
    parameters_model = ApplyDiffParams
    requires_confirmation = True
    category = "filesystem"
    # Same-path patches in one batch must serialize (no TOCTOU race).
    batch_serialize_by_path = True

    # How far from the stated line number to search for a matching hunk
    SEARCH_WINDOW = 50

    def preview(self, arguments: dict[str, Any], original: Optional[str]) -> Optional[ToolPreview]:
        """Show the model's own unified diff verbatim (the patch it asked to apply).

        Rendering an after-the-fact before/after diff would hide the actual
        patch text the user is approving, so the raw diff is surfaced directly.
        """
        if original is None:
            return None
        raw = arguments.get("diff", "")
        diff_str = str(raw) if raw is not None else ""
        if not diff_str:
            return None
        return ToolPreview(rendered_diff=diff_str)

    async def execute(self, path: str, diff: str) -> dict[str, Any]:  # type: ignore[override]
        """Apply a unified diff to a file."""
        try:
            path_obj = Path(path).expanduser()

            lock = await get_lock_manager().get_file_lock(str(path_obj))
            async with lock:
                if not path_obj.exists():
                    return {
                        "success": False,
                        "error": f"File not found: {path}",
                        "hint": "Use read_file to verify file contents before creating a diff.",
                    }

                if _is_path_protected(path_obj):
                    return {
                        "success": False,
                        "error": f"Cannot modify protected path: {path}",
                    }
                scope_err = _enforce_project_scope(path_obj, "apply_diff")
                if scope_err:
                    return scope_err
                symlink_err = _reject_symlink_leaf(path_obj, "apply_diff to")
                if symlink_err:
                    return symlink_err

                def _readlines() -> list[str]:
                    with _safe_open_no_symlink(path_obj, "r") as f:
                        return f.readlines()

                try:
                    original_lines = await asyncio.to_thread(_readlines)
                except OSError as e:
                    return {
                        "success": False,
                        "error": f"Could not open {path}: {e}",
                        "error_code": ToolErrorCode.SYMLINK
                        if "loop" in str(e).lower()
                        else ToolErrorCode.IO,
                    }

                # Clean up markdown code blocks if the LLM provided them
                diff = diff.strip()
                if diff.startswith("```"):
                    parts = diff.split("\n", 1)
                    if len(parts) == 2:
                        diff = parts[1]
                    if diff.endswith("```"):
                        diff = diff[:-3].rstrip()

                # Normalize CRLF / stray \r before parsing
                normalized_diff = diff.replace("\r\n", "\n").replace("\r", "\n")

                hunks = self._parse_hunks(normalized_diff)
                if not hunks:
                    return {
                        "success": False,
                        "error": "No valid hunks found in diff. Use @@ -start,count +start,count @@ format.",
                    }

                # Create backup BEFORE modifying — consistent with other write tools
                get_backup_store().backup_file(str(path_obj), "modify")

                result_lines = list(original_lines)
                hunks_applied = 0

                for hunk in reversed(hunks):
                    expected_start = hunk["start"] - 1  # 0-indexed
                    old_lines = hunk["old_lines"]
                    new_lines = hunk["new_lines"]

                    # Pure insertion (no old lines) — just insert at the position
                    if not old_lines:
                        insert_pos = min(expected_start, len(result_lines))
                        result_lines[insert_pos:insert_pos] = [
                            line if line.endswith("\n") else line + "\n" for line in new_lines
                        ]
                        hunks_applied += 1
                        continue

                    # Search for the matching position (exact first, then nearby)
                    match_pos = self._find_hunk_position(
                        result_lines, old_lines, expected_start, self.SEARCH_WINDOW
                    )

                    if match_pos is None:
                        file_slice = result_lines[expected_start : expected_start + len(old_lines)]
                        expected_preview = "\n".join(f"  {line}" for line in old_lines[:6])
                        actual_preview = "\n".join(
                            f"  {line.rstrip(chr(10))}" for line in file_slice[:6]
                        )
                        if len(old_lines) > 6:
                            expected_preview += "\n  ..."
                        if len(file_slice) > 6:
                            actual_preview += "\n  ..."
                        return {
                            "success": False,
                            "error": (
                                f"Hunk at line {hunk['start']} does not match file contents "
                                f"(searched ±{self.SEARCH_WINDOW} lines).\n"
                                f"Expected:\n{expected_preview}\n"
                                f"Found at line {hunk['start']}:\n{actual_preview}"
                            ),
                            "hint": "Read the file first and create the diff based on actual content.",
                        }

                    result_lines[match_pos : match_pos + len(old_lines)] = [
                        line if line.endswith("\n") else line + "\n" for line in new_lines
                    ]
                    hunks_applied += 1

                # Write to a temp file then atomically replace to avoid
                # leaving the target in a partial-write state.
                joined = "".join(result_lines)
                try:
                    await asyncio.to_thread(_atomic_write_file, path_obj, joined)
                except OSError as e:
                    return {
                        "success": False,
                        "error": f"Could not write {path}: {e}",
                        "error_code": ToolErrorCode.SYMLINK
                        if "loop" in str(e).lower()
                        else ToolErrorCode.IO,
                    }

                _emit_diff(
                    path_obj,
                    "".join(original_lines),
                    "".join(result_lines),
                )

                return {
                    "success": True,
                    "path": str(path_obj),
                    "hunks_applied": hunks_applied,
                    "lines_before": len(original_lines),
                    "lines_after": len(result_lines),
                }

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }

    @staticmethod
    def _find_hunk_position(
        file_lines: list[str],
        old_lines: list[str],
        expected_start: int,
        search_window: int,
    ) -> Optional[int]:
        """Find where a hunk's old_lines match in the file.

        Tries the expected position first, then searches within ±search_window.
        Comparison strips trailing whitespace so minor trailing-space
        differences between the diff and the file don't cause failures.

        Returns the 0-indexed start position, or None if no match.
        """
        old_normalized = [line.rstrip() for line in old_lines]
        n_old = len(old_normalized)

        def _matches_at(pos: int) -> bool:
            if pos < 0 or pos + n_old > len(file_lines):
                return False
            for file_line, old_line in zip(file_lines[pos : pos + n_old], old_normalized):
                if file_line.rstrip() != old_line:
                    return False
            return True

        if _matches_at(expected_start):
            return expected_start

        for offset in range(1, search_window + 1):
            if _matches_at(expected_start - offset):
                return expected_start - offset
            if _matches_at(expected_start + offset):
                return expected_start + offset

        return None

    @staticmethod
    def _parse_hunks(diff_text: str) -> list[dict[str, Any]]:
        """Parse unified diff text into hunks.

        Handles common LLM quirks:
        - Missing space prefix on context lines
        - Trailing blank lines from JSON serialization
        - ``---`` / ``+++`` file headers (skipped outside hunks)
        """
        hunks: list[dict[str, Any]] = []
        hunk_header_re = re.compile(r"^@@\s*-(\d+)(?:,\d+)?\s*\+\d+(?:,\d+)?\s*@@")

        # Strip trailing blank lines that result from split() on trailing \n
        lines = diff_text.split("\n")
        while lines and lines[-1] == "":
            lines.pop()

        i = 0
        while i < len(lines):
            match = hunk_header_re.match(lines[i])
            if match:
                start_line = int(match.group(1))
                old_lines: list[str] = []
                new_lines: list[str] = []
                i += 1

                while i < len(lines):
                    line = lines[i]
                    if hunk_header_re.match(line):
                        break
                    if line.startswith("-"):
                        old_lines.append(line[1:])
                    elif line.startswith("+"):
                        new_lines.append(line[1:])
                    elif line.startswith(" "):
                        old_lines.append(line[1:])
                        new_lines.append(line[1:])
                    elif line == "\\ No newline at end of file":
                        pass
                    else:
                        # Unprefixed line — treat as context (common LLM omission)
                        old_lines.append(line)
                        new_lines.append(line)
                    i += 1

                if old_lines or new_lines:
                    hunks.append(
                        {
                            "start": start_line,
                            "old_lines": old_lines,
                            "new_lines": new_lines,
                        }
                    )
            else:
                i += 1

        return hunks
