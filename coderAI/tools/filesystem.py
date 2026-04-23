"""Filesystem tools for file operations."""

import asyncio
import difflib
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .base import Tool
from .undo import backup_store
from ..config import config_manager
from ..events import event_emitter
from ..locks import resource_manager


def _emit_diff(path_obj: Path, before: str, after: str) -> None:
    """Compute and emit a unified diff event if the content changed."""
    if before == after:
        return
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path_obj.name}",
            tofile=f"b/{path_obj.name}",
            n=3,
        )
    )
    if diff:
        event_emitter.emit("file_diff", path=str(path_obj), diff=diff)

# Defaults (overridden by config if set)
DEFAULT_MAX_FILE_SIZE = 1_048_576
DEFAULT_MAX_GLOB_RESULTS = 200


def _get_max_file_size() -> int:
    """Get max file size from config."""
    try:
        return config_manager.load().max_file_size
    except Exception:
        return DEFAULT_MAX_FILE_SIZE


def _get_max_glob_results() -> int:
    """Get max glob results from config."""
    try:
        return config_manager.load().max_glob_results
    except Exception:
        return DEFAULT_MAX_GLOB_RESULTS


# Paths under $HOME that tools should never write to.
PROTECTED_HOME_PATHS = [
    ".ssh",
    ".gnupg",
    ".aws",
    ".config/gcloud",
    ".kube",
    ".docker",
    ".bash_history",
    ".zsh_history",
]

# Absolute system paths that tools should never write to. If the process
# happens to run with elevated privileges, the OS-level write permission is
# NOT a safety net — we refuse these up front.
PROTECTED_SYSTEM_PATHS = [
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/boot",
    "/System",   # macOS system dir
    "/Library",  # macOS shared library dir
    "/private/etc",
    "/var/log",
    "/root",
]


def _is_path_protected(path: Path) -> bool:
    """Check if a path targets a protected location (home or system)."""
    resolved = path.resolve()
    home = Path.home()
    for protected in PROTECTED_HOME_PATHS:
        protected_path = (home / protected).resolve()
        try:
            resolved.relative_to(protected_path)
            return True
        except ValueError:
            continue
    for system in PROTECTED_SYSTEM_PATHS:
        system_path = Path(system)
        if not system_path.exists():
            continue
        try:
            resolved.relative_to(system_path.resolve())
            return True
        except ValueError:
            continue
    return False


class ReadFileParams(BaseModel):
    path: str = Field(..., description="Path to the file to read")
    start_line: Optional[int] = Field(None, description="Optional starting line number (1-indexed)")
    end_line: Optional[int] = Field(None, description="Optional ending line number (1-indexed)")


class ReadFileTool(Tool):
    """Tool for reading file contents."""

    name = "read_file"
    description = "Read the contents of a file"
    parameters_model = ReadFileParams
    is_read_only = True

    async def execute(
        self, path: str, start_line: int = None, end_line: int = None
    ) -> Dict[str, Any]:
        """Read file contents with size limit."""
        try:
            path_obj = Path(path).expanduser()
            if not path_obj.exists():
                return {
                    "success": False,
                    "error": f"File not found: {path}",
                    "error_code": "not_found",
                    "hint": "Use list_directory or glob_search to find the correct path.",
                }

            if not path_obj.is_file():
                return {
                    "success": False,
                    "error": f"Not a file: {path}",
                    "hint": "Use list_directory for directories.",
                }

            # Check file size before reading
            file_size = path_obj.stat().st_size
            max_file_size = _get_max_file_size()
            if file_size > max_file_size:
                return {
                    "success": False,
                    "error": f"File too large: {file_size:,} bytes (limit: {max_file_size:,} bytes).",
                    "error_code": "too_large",
                    "hint": "Use start_line and end_line to read a specific range, or use grep to search.",
                }

            with open(path_obj, "r", encoding="utf-8") as f:
                if start_line is not None or end_line is not None:
                    lines = f.readlines()
                    start = (start_line - 1) if start_line else 0
                    end = end_line if end_line else len(lines)
                    content = "".join(lines[start:end])
                else:
                    content = f.read()

            return {
                "success": True,
                "path": str(path_obj),
                "content": content,
                "lines": len(content.split("\n")),
                "size_bytes": file_size,
            }
        except UnicodeDecodeError:
            return {
                "success": False,
                "error": f"Cannot read binary file: {path}",
                "hint": "This appears to be a binary file. Use run_command with appropriate tools like 'file', 'hexdump', etc.",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


class WriteFileParams(BaseModel):
    path: str = Field(..., description="Path to the file to write")
    content: str = Field(..., description="Content to write to the file")
    append: bool = Field(
        False, description="Append to file instead of overwriting (default: false)"
    )


class WriteFileTool(Tool):
    """Tool for writing/creating files."""

    name = "write_file"
    description = "Write content to a file (creates, overwrites, or appends). Protected system paths are blocked."
    parameters_model = WriteFileParams
    requires_confirmation = True

    async def execute(self, path: str, content: str, append: bool = False) -> Dict[str, Any]:
        """Write content to file with path protection."""
        try:
            path_obj = Path(path).expanduser()
            
            # Acquire lock for this specific file
            lock = await resource_manager.get_file_lock(str(path_obj))
            async with lock:
                # Check path protection
                if _is_path_protected(path_obj):
                    return {
                        "success": False,
                        "error": f"Cannot write to protected path: {path}",
                        "error_code": "permission_denied",
                        "hint": "This path is protected for security. Choose a different location.",
                    }

                path_obj.parent.mkdir(parents=True, exist_ok=True)

                # Read before-content for diff (skip binary / append mode)
                before_content: Optional[str] = None
                if not append:
                    if path_obj.exists():
                        backup_store.backup_file(str(path_obj), "modify")
                        try:
                            before_content = path_obj.read_text(encoding="utf-8")
                        except Exception:
                            before_content = None
                    else:
                        backup_store.backup_file(str(path_obj), "create")
                        before_content = ""
                else:
                    backup_store.backup_file(str(path_obj), "modify") if path_obj.exists() else backup_store.backup_file(str(path_obj), "create")

                mode = "a" if append else "w"
                with open(path_obj, mode, encoding="utf-8") as f:
                    f.write(content)

                # Emit diff for non-append text writes
                if before_content is not None:
                    _emit_diff(path_obj, before_content, content)

                return {
                    "success": True,
                    "path": str(path_obj),
                    "bytes_written": len(content.encode("utf-8")),
                    "mode": "append" if append else "write",
                }
        except Exception as e:
            return {"success": False, "error": str(e)}


class SearchReplaceParams(BaseModel):
    path: str = Field(..., description="Path to the file")
    search: str = Field(..., description="Text to search for")
    replace: str = Field(..., description="Text to replace with")
    replace_all: bool = Field(False, description="Replace all occurrences (default: first only)")


class SearchReplaceTool(Tool):
    """Tool for search and replace in files."""

    name = "search_replace"
    description = "Search for text in a file and replace it"
    parameters_model = SearchReplaceParams
    requires_confirmation = True

    async def execute(
        self, path: str, search: str, replace: str, replace_all: bool = False
    ) -> Dict[str, Any]:
        """Search and replace in file with protection."""
        try:
            if not path or not str(path).strip():
                return {
                    "success": False,
                    "error": "path is required and must be a non-empty file path.",
                }
            if search == "":
                return {
                    "success": False,
                    "error": "search text must be non-empty.",
                }

            path_obj = Path(path).expanduser()

            lock = await resource_manager.get_file_lock(str(path_obj))
            async with lock:
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

                if _is_path_protected(path_obj):
                    return {
                        "success": False,
                        "error": f"Cannot modify protected path: {path}",
                    }

                with open(path_obj, "r", encoding="utf-8") as f:
                    content = f.read()

                if search not in content:
                    return {
                        "success": False,
                        "error": "Search text not found in file",
                        "hint": "Use text_search or grep to verify the exact text in the file.",
                    }

                # Create backup for undo support
                backup_store.backup_file(str(path_obj), "modify")

                if replace_all:
                    new_content = content.replace(search, replace)
                    count = content.count(search)
                else:
                    new_content = content.replace(search, replace, 1)
                    count = 1

                with open(path_obj, "w", encoding="utf-8") as f:
                    f.write(new_content)

                _emit_diff(path_obj, content, new_content)

                return {
                    "success": True,
                    "path": str(path_obj),
                    "replacements": count,
                }
        except Exception as e:
            return {"success": False, "error": str(e)}


class ListDirectoryParams(BaseModel):
    path: str = Field(..., description="Path to the directory")


class ListDirectoryTool(Tool):
    """Tool for listing directory contents."""

    name = "list_directory"
    description = "List files and directories in a path"
    parameters_model = ListDirectoryParams
    is_read_only = True

    async def execute(self, path: str) -> Dict[str, Any]:
        """List directory contents."""
        try:
            path_obj = Path(path).expanduser()
            if not path_obj.exists():
                return {
                    "success": False,
                    "error": f"Directory not found: {path}",
                    "hint": "Check the parent directory with list_directory.",
                }

            if not path_obj.is_dir():
                return {
                    "success": False,
                    "error": f"Not a directory: {path}",
                    "hint": "Use read_file to read file contents.",
                }

            entries = []
            for entry in sorted(path_obj.iterdir()):
                entries.append(
                    {
                        "name": entry.name,
                        "type": "directory" if entry.is_dir() else "file",
                        "size": entry.stat().st_size if entry.is_file() else 0,
                    }
                )

            return {
                "success": True,
                "path": str(path_obj),
                "entries": entries,
                "count": len(entries),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


class GlobSearchParams(BaseModel):
    pattern: str = Field(..., description="Glob pattern (e.g., '**/*.py', '*.txt')")
    base_path: str = Field(".", description="Base path to search from (default: current directory)")


class GlobSearchTool(Tool):
    """Tool for finding files using glob patterns."""

    name = "glob_search"
    description = "Find files matching a glob pattern"
    parameters_model = GlobSearchParams
    is_read_only = True

    async def execute(self, pattern: str, base_path: str = ".") -> Dict[str, Any]:
        """Find files matching pattern with result limit."""
        try:
            base = Path(base_path).expanduser()
            if not base.exists():
                return {
                    "success": False,
                    "error": f"Base path not found: {base_path}",
                    "hint": "Check the path with list_directory.",
                }

            max_glob_results = _get_max_glob_results()
            matches = []
            total_matches = 0
            for match in base.glob(pattern):
                if match.is_file():
                    # Skip common ignore patterns
                    if any(
                        p in match.parts
                        for p in [".git", "node_modules", "__pycache__", ".venv", "venv"]
                    ):
                        continue

                    total_matches += 1
                    if len(matches) < max_glob_results:
                        matches.append(
                            str(match.relative_to(base) if match.is_relative_to(base) else match)
                        )

            result = {
                "success": True,
                "pattern": pattern,
                "matches": matches,
                "count": len(matches),
            }

            if total_matches > max_glob_results:
                result["note"] = (
                    f"Showing {max_glob_results} of {total_matches} total matches. "
                    "Use a more specific pattern to narrow results."
                )

            return result
        except Exception as e:
            return {"success": False, "error": str(e)}


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

    # How far from the stated line number to search for a matching hunk
    SEARCH_WINDOW = 50

    async def execute(self, path: str, diff: str) -> Dict[str, Any]:
        """Apply a unified diff to a file."""
        try:
            path_obj = Path(path).expanduser()
            
            lock = await resource_manager.get_file_lock(str(path_obj))
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

                with open(path_obj, "r", encoding="utf-8") as f:
                    original_lines = f.readlines()

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

                backup_store.backup_file(str(path_obj), "modify")

                with open(path_obj, "w", encoding="utf-8") as f:
                    f.writelines(result_lines)

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
            return {"success": False, "error": str(e)}

    @staticmethod
    def _find_hunk_position(
        file_lines: List[str],
        old_lines: List[str],
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
    def _parse_hunks(diff_text: str) -> List[Dict[str, Any]]:
        """Parse unified diff text into hunks.

        Handles common LLM quirks:
        - Missing space prefix on context lines
        - Trailing blank lines from JSON serialization
        - ``---`` / ``+++`` file headers (skipped outside hunks)
        """
        hunks: List[Dict[str, Any]] = []
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
                old_lines: List[str] = []
                new_lines: List[str] = []
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


# ---------------------------------------------------------------------------
# Additional filesystem tools: move, copy, delete, mkdir
# ---------------------------------------------------------------------------


class MoveFileParams(BaseModel):
    source: str = Field(..., description="Source file or directory path")
    destination: str = Field(..., description="Destination path (file or directory)")
    overwrite: bool = Field(False, description="Overwrite the destination if it already exists")


class MoveFileTool(Tool):
    """Move or rename a file or directory."""

    name = "move_file"
    description = (
        "Move or rename a file or directory. Set overwrite=true to replace an existing "
        "destination; by default the operation fails if the destination exists."
    )
    category = "fs"
    parameters_model = MoveFileParams
    requires_confirmation = True

    async def execute(
        self, source: str, destination: str, overwrite: bool = False
    ) -> Dict[str, Any]:
        import shutil as _shutil

        try:
            src = Path(source)
            dst = Path(destination)

            if not src.exists():
                return {"success": False, "error": f"Source does not exist: {source}"}

            if _is_path_protected(dst):
                return {"success": False, "error": f"Destination is in a protected path: {destination}"}

            if dst.exists() and not overwrite:
                return {
                    "success": False,
                    "error": f"Destination already exists: {destination}. Set overwrite=true to replace it.",
                }

            dst.parent.mkdir(parents=True, exist_ok=True)

            def _move():
                _shutil.move(str(src), str(dst))

            await asyncio.to_thread(_move)
            return {
                "success": True,
                "source": str(src),
                "destination": str(dst),
                "message": f"Moved '{src}' → '{dst}'",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


class CopyFileParams(BaseModel):
    source: str = Field(..., description="Source file or directory path")
    destination: str = Field(..., description="Destination path")
    overwrite: bool = Field(False, description="Overwrite the destination if it already exists")


class CopyFileTool(Tool):
    """Copy a file or directory tree."""

    name = "copy_file"
    description = (
        "Copy a file or directory to a new location. For directories, copies the entire tree. "
        "Set overwrite=true to replace an existing destination."
    )
    category = "fs"
    parameters_model = CopyFileParams
    requires_confirmation = True

    async def execute(
        self, source: str, destination: str, overwrite: bool = False
    ) -> Dict[str, Any]:
        import shutil as _shutil

        try:
            src = Path(source)
            dst = Path(destination)

            if not src.exists():
                return {"success": False, "error": f"Source does not exist: {source}"}

            if _is_path_protected(dst):
                return {"success": False, "error": f"Destination is in a protected path: {destination}"}

            if dst.exists() and not overwrite:
                return {
                    "success": False,
                    "error": f"Destination already exists: {destination}. Set overwrite=true to replace it.",
                }

            dst.parent.mkdir(parents=True, exist_ok=True)

            def _copy():
                if src.is_dir():
                    if dst.exists():
                        _shutil.rmtree(str(dst))
                    _shutil.copytree(str(src), str(dst))
                else:
                    _shutil.copy2(str(src), str(dst))

            await asyncio.to_thread(_copy)
            return {
                "success": True,
                "source": str(src),
                "destination": str(dst),
                "message": f"Copied '{src}' → '{dst}'",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


class DeleteFileParams(BaseModel):
    path: str = Field(..., description="File or directory path to delete")
    recursive: bool = Field(False, description="Delete directories and their contents recursively")


class DeleteFileTool(Tool):
    """Delete a file or directory."""

    name = "delete_file"
    description = (
        "Delete a file or empty directory. Set recursive=true to delete a directory and all "
        "its contents. Protected system and home paths are always refused."
    )
    category = "fs"
    parameters_model = DeleteFileParams
    requires_confirmation = True

    async def execute(self, path: str, recursive: bool = False) -> Dict[str, Any]:
        import shutil as _shutil

        try:
            target = Path(path)

            if not target.exists():
                return {"success": False, "error": f"Path does not exist: {path}"}

            if _is_path_protected(target):
                return {"success": False, "error": f"Refusing to delete protected path: {path}"}

            def _delete():
                if target.is_dir():
                    if recursive:
                        _shutil.rmtree(str(target))
                    else:
                        target.rmdir()
                else:
                    target.unlink()

            await asyncio.to_thread(_delete)
            return {
                "success": True,
                "path": str(target),
                "message": f"Deleted '{target}'",
            }
        except OSError as e:
            if "Directory not empty" in str(e) or e.errno == 39:
                return {
                    "success": False,
                    "error": f"Directory not empty: {path}. Set recursive=true to delete it and its contents.",
                }
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": str(e)}


class CreateDirectoryParams(BaseModel):
    path: str = Field(..., description="Directory path to create")
    parents: bool = Field(True, description="Create parent directories as needed (default: true)")


class CreateDirectoryTool(Tool):
    """Create one or more directories."""

    name = "create_directory"
    description = (
        "Create a directory (and any missing parent directories by default). "
        "Succeeds silently if the directory already exists."
    )
    category = "fs"
    parameters_model = CreateDirectoryParams

    async def execute(self, path: str, parents: bool = True) -> Dict[str, Any]:
        try:
            target = Path(path)

            if _is_path_protected(target):
                return {"success": False, "error": f"Refusing to create directory in protected path: {path}"}

            def _mkdir():
                target.mkdir(parents=parents, exist_ok=True)

            await asyncio.to_thread(_mkdir)
            return {
                "success": True,
                "path": str(target.resolve()),
                "message": f"Directory created: '{target}'",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
