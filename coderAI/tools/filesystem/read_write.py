"""File read and write tools."""

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from coderAI.core.tool_error_codes import ToolErrorCode
from coderAI.system.locks import resource_manager
from coderAI.tools.base import Tool, ToolPreview
from coderAI.tools.undo import backup_store

from coderAI.tools.filesystem._guards import (
    _atomic_write_file,
    _emit_diff,
    _enforce_project_scope,
    _get_max_file_size,
    _is_path_protected,
    _reject_symlink_leaf,
    _safe_open_no_symlink,
)

logger = logging.getLogger(__name__)


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
    category = "filesystem"

    # Optional per-session FileReadCache; wired by Agent after registry build.
    read_cache = None

    async def execute(  # type: ignore[override]
        self, path: str, start_line: Optional[int] = None, end_line: Optional[int] = None
    ) -> dict[str, Any]:
        """Read file contents with size limit."""
        try:
            path_obj = Path(path).expanduser()
            scope_err = _enforce_project_scope(path_obj, "read")
            if scope_err:
                return scope_err
            if not path_obj.exists():
                return {
                    "success": False,
                    "error": f"File not found: {path}",
                    "error_code": ToolErrorCode.NOT_FOUND,
                    "hint": "Use list_directory or glob_search to find the correct path.",
                }

            if not path_obj.is_file():
                return {
                    "success": False,
                    "error": f"Not a file: {path}",
                    "hint": "Use list_directory for directories.",
                }

            # Refuse a symlink leaf and open with O_NOFOLLOW (below): the
            # scope check above resolves through symlinks, so a link that
            # currently points inside the project could be swapped to target
            # /etc/passwd between the check and the open. Mirrors the write path.
            symlink_err = _reject_symlink_leaf(path_obj, "read")
            if symlink_err:
                return symlink_err

            # Check file size before reading
            stat = path_obj.stat()
            file_size = stat.st_size
            mtime = stat.st_mtime
            max_file_size = _get_max_file_size()
            if file_size > max_file_size:
                return {
                    "success": False,
                    "error": f"File too large: {file_size:,} bytes (limit: {max_file_size:,} bytes).",
                    "error_code": ToolErrorCode.TOO_LARGE,
                    "hint": "Use start_line and end_line to read a specific range, or use grep to search.",
                }

            is_partial_read = start_line is not None or end_line is not None

            # Consult the per-session read cache for repeat reads of unchanged
            # full files. Partial reads bypass — start/end may move turn over
            # turn even when the file is byte-identical.
            cache = self.read_cache
            cache_key = str(path_obj.resolve())
            if cache is not None and not is_partial_read:
                prev_turn = cache.check(cache_key, mtime, file_size)
                if prev_turn is not None:
                    return {
                        "success": True,
                        "path": str(path_obj),
                        "cached": True,
                        "content": f"[unchanged since previous read at turn {prev_turn}]",
                        "size_bytes": file_size,
                    }

            def _read() -> str:
                # Offloaded to a worker thread so a large file read doesn't
                # block the event loop (UnicodeDecodeError propagates out and
                # is handled by the caller's except clause). O_NOFOLLOW closes
                # the TOCTOU gap after the _reject_symlink_leaf check above.
                with _safe_open_no_symlink(path_obj, "r") as f:
                    if is_partial_read:
                        lines = f.readlines()
                        start = (start_line - 1) if start_line else 0
                        end = end_line if end_line else len(lines)
                        return "".join(lines[start:end])
                    return f.read()

            content = await asyncio.to_thread(_read)

            line_count = content.count("\n")
            # A file with no trailing newline still has 1 line of content,
            # but a truly empty file has 0 lines.
            if content and not content.endswith("\n"):
                line_count += 1

            if cache is not None and not is_partial_read:
                cache.record(cache_key, mtime, file_size)

            return {
                "success": True,
                "path": str(path_obj),
                "content": content,
                "lines": line_count,
                "size_bytes": file_size,
            }
        except UnicodeDecodeError:
            return {
                "success": False,
                "error": f"Cannot read binary file: {path}",
                "hint": "This appears to be a binary file. Use run_command with appropriate tools like 'file', 'hexdump', etc.",
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


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
    category = "filesystem"
    # Can clobber arbitrary files — no blanket allow; scope by path/subtree.
    high_risk_no_blanket = True
    approval_scope = "path"
    # Same-path writes in one batch must serialize (no TOCTOU race).
    batch_serialize_by_path = True

    def preview(self, arguments: dict[str, Any], original: Optional[str]) -> Optional[ToolPreview]:
        """Resulting file content: appended onto ``original`` or a full overwrite."""
        content = str(arguments.get("content", "") or "")
        if arguments.get("append"):
            return ToolPreview(new_content=(original or "") + content)
        return ToolPreview(new_content=content)

    async def execute(self, path: str, content: str, append: bool = False) -> dict[str, Any]:  # type: ignore[override]
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
                        "error_code": ToolErrorCode.PERMISSION_DENIED,
                        "hint": "This path is protected for security. Choose a different location.",
                    }
                scope_err = _enforce_project_scope(path_obj, "write_file")
                if scope_err:
                    return scope_err
                # Refuse a symlink leaf — either the file already exists as a
                # symlink (likely TOCTOU bait) or the agent is asking us to
                # create one, neither of which we want to honour silently.
                if path_obj.exists():
                    symlink_err = _reject_symlink_leaf(path_obj, "write to")
                    if symlink_err:
                        return symlink_err

                path_obj.parent.mkdir(parents=True, exist_ok=True)

                # Re-check symlink after parent creation (mitigates TOCTOU
                # where a symlink could be placed at path_obj between the
                # initial check above and the mkdir call).
                if path_obj.exists():
                    symlink_err2 = _reject_symlink_leaf(path_obj, "write to")
                    if symlink_err2:
                        return symlink_err2

                # Capture old file size for accurate bytes_written on append
                old_size = path_obj.stat().st_size if path_obj.exists() and append else 0

                # Read before-content for diff (skip binary / append mode)
                before_content: Optional[str] = None
                if not append:
                    if path_obj.exists():
                        await asyncio.to_thread(backup_store.backup_file, str(path_obj), "modify")
                        try:
                            before_content = await asyncio.to_thread(
                                path_obj.read_text, encoding="utf-8"
                            )
                        except Exception:
                            # Binary or unreadable file → skip the diff preview;
                            # the write itself proceeds (backup already taken).
                            before_content = None
                    else:
                        await asyncio.to_thread(backup_store.backup_file, str(path_obj), "create")
                        before_content = ""
                else:
                    op = "modify" if path_obj.exists() else "create"
                    backup_result = await asyncio.to_thread(
                        backup_store.backup_file, str(path_obj), op
                    )
                    if isinstance(backup_result, dict) and backup_result.get("error"):
                        logger.warning("Backup before append failed: %s", backup_result["error"])

                mode = "a" if append else "w"

                def _do_write() -> None:
                    # Blocking disk write offloaded to a worker thread.
                    if mode == "w":
                        _atomic_write_file(path_obj, content)
                    else:
                        with _safe_open_no_symlink(path_obj, mode) as f:
                            f.write(content)

                try:
                    await asyncio.to_thread(_do_write)
                except OSError as e:
                    return {
                        "success": False,
                        "error": f"Could not write {path}: {e}",
                        "error_code": ToolErrorCode.SYMLINK
                        if "loop" in str(e).lower()
                        else ToolErrorCode.IO,
                    }

                # Emit diff for non-append text writes
                if before_content is not None:
                    _emit_diff(path_obj, before_content, content)

                bytes_written = path_obj.stat().st_size - old_size

                return {
                    "success": True,
                    "path": str(path_obj),
                    "bytes_written": bytes_written,
                    "mode": "append" if append else "write",
                }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }
