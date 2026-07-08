"""Directory browsing and file management tools: list, glob, move, copy, delete, mkdir."""

import asyncio
import os
import shutil as _shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from coderAI.core.tool_error_codes import ToolErrorCode
from coderAI.tools.base import Tool
from coderAI.tools.undo import backup_store

from coderAI.tools.filesystem._guards import (
    _enforce_project_scope,
    _get_max_glob_results,
    _is_path_protected,
    _reject_symlink_leaf,
)


class ListDirectoryParams(BaseModel):
    path: str = Field(..., description="Path to the directory")


class ListDirectoryTool(Tool):
    """Tool for listing directory contents."""

    name = "list_directory"
    description = "List files and directories in a path"
    parameters_model = ListDirectoryParams
    is_read_only = True
    category = "filesystem"

    async def execute(self, path: str) -> dict[str, Any]:  # type: ignore[override]
        """List directory contents."""
        try:
            path_obj = Path(path).expanduser()
            scope_err = _enforce_project_scope(path_obj, "list")
            if scope_err:
                return scope_err
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
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


class GlobSearchParams(BaseModel):
    pattern: str = Field(..., description="Glob pattern (e.g., '**/*.py', '*.txt')")
    base_path: str = Field(".", description="Base path to search from (default: current directory)")


class GlobSearchTool(Tool):
    """Tool for finding files using glob patterns."""

    name = "glob_search"
    description = "Find files matching a glob pattern"
    parameters_model = GlobSearchParams
    is_read_only = True
    category = "filesystem"

    async def execute(self, pattern: str, base_path: str = ".") -> dict[str, Any]:  # type: ignore[override]
        """Find files matching pattern with result limit."""
        try:
            base = Path(base_path).expanduser()
            scope_err = _enforce_project_scope(base, "glob_search")
            if scope_err:
                return scope_err
            if not base.exists():
                return {
                    "success": False,
                    "error": f"Base path not found: {base_path}",
                    "hint": "Check the path with list_directory.",
                }

            max_glob_results = _get_max_glob_results()
            matches: list[str] = []
            total_matches = 0
            for match in base.glob(pattern):
                try:
                    is_file = match.is_file()
                except OSError:
                    continue
                if is_file:
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
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


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
    category = "filesystem"
    parameters_model = MoveFileParams
    requires_confirmation = True
    # Can clobber/relocate arbitrary files — no blanket allow; scope by path.
    high_risk_no_blanket = True
    approval_scope = "path"

    async def execute(  # type: ignore[override]
        self, source: str, destination: str, overwrite: bool = False
    ) -> dict[str, Any]:

        try:
            src = Path(os.path.expanduser(source))
            dst = Path(os.path.expanduser(destination))

            if not src.exists():
                return {"success": False, "error": f"Source does not exist: {source}"}

            if _is_path_protected(src):
                return {"success": False, "error": f"Source is in a protected path: {source}"}
            scope_err = _enforce_project_scope(src, "move/copy")
            if scope_err:
                return scope_err
            if _is_path_protected(dst):
                return {
                    "success": False,
                    "error": f"Destination is in a protected path: {destination}",
                }
            scope_err = _enforce_project_scope(dst, "move/copy")
            if scope_err:
                return scope_err
            # Refuse symlink leaves on either side. ``_is_path_protected``
            # resolves through symlinks, so a swap between check and move
            # could otherwise redirect the operation onto a protected target.
            symlink_err = _reject_symlink_leaf(src, "move from") or _reject_symlink_leaf(
                dst, "move to"
            )
            if symlink_err:
                return symlink_err

            if dst.exists() and not overwrite:
                return {
                    "success": False,
                    "error": f"Destination already exists: {destination}. Set overwrite=true to replace it.",
                }

            dst.parent.mkdir(parents=True, exist_ok=True)

            # Backup source (it will be removed) and destination (if overwritten)
            if src.is_file():
                await asyncio.to_thread(backup_store.backup_file, str(src), "delete")
            if dst.exists() and dst.is_file():
                await asyncio.to_thread(backup_store.backup_file, str(dst), "modify")

            def _move():
                # Re-check both leaves right before the move to guard against a
                # TOCTOU swap between the validation above and the op itself
                # (mirrors DeleteFileTool._delete).
                symlink_err2 = _reject_symlink_leaf(src, "move from") or _reject_symlink_leaf(
                    dst, "move to"
                )
                if symlink_err2:
                    raise OSError("Path was replaced by a symlink after validation")
                _shutil.move(str(src), str(dst))

            await asyncio.to_thread(_move)
            return {
                "success": True,
                "source": str(src),
                "destination": str(dst),
                "message": f"Moved '{src}' → '{dst}'",
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


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
    category = "filesystem"
    parameters_model = CopyFileParams
    requires_confirmation = True

    async def execute(  # type: ignore[override]
        self, source: str, destination: str, overwrite: bool = False
    ) -> dict[str, Any]:

        try:
            src = Path(os.path.expanduser(source))
            dst = Path(os.path.expanduser(destination))

            if not src.exists():
                return {"success": False, "error": f"Source does not exist: {source}"}

            if _is_path_protected(src):
                return {"success": False, "error": f"Source is in a protected path: {source}"}
            scope_err = _enforce_project_scope(src, "move/copy")
            if scope_err:
                return scope_err
            if _is_path_protected(dst):
                return {
                    "success": False,
                    "error": f"Destination is in a protected path: {destination}",
                }
            scope_err = _enforce_project_scope(dst, "move/copy")
            if scope_err:
                return scope_err
            # ``shutil.copy2`` follows symlinks by default and copies the
            # *target's* contents — a swapped src symlink would otherwise let
            # us copy ``/etc/passwd`` into the project. Refuse symlink leaves
            # on either side.
            symlink_err = _reject_symlink_leaf(src, "copy from") or _reject_symlink_leaf(
                dst, "copy to"
            )
            if symlink_err:
                return symlink_err

            if dst.exists() and not overwrite:
                return {
                    "success": False,
                    "error": f"Destination already exists: {destination}. Set overwrite=true to replace it.",
                }

            dst.parent.mkdir(parents=True, exist_ok=True)

            # Backup destination if it will be overwritten
            if dst.exists() and dst.is_file():
                await asyncio.to_thread(backup_store.backup_file, str(dst), "modify")

            def _copy():
                # Re-check both leaves right before the copy to guard against a
                # TOCTOU swap between the validation above and the op itself
                # (mirrors DeleteFileTool._delete).
                symlink_err2 = _reject_symlink_leaf(src, "copy from") or _reject_symlink_leaf(
                    dst, "copy to"
                )
                if symlink_err2:
                    raise OSError("Path was replaced by a symlink after validation")
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
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


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
    category = "filesystem"
    parameters_model = DeleteFileParams
    requires_confirmation = True
    # Irreversible removal — no blanket allow; scope by path/subtree.
    high_risk_no_blanket = True
    approval_scope = "path"
    # Same-path operations in one batch must serialize (no TOCTOU race).
    batch_serialize_by_path = True

    async def execute(self, path: str, recursive: bool = False) -> dict[str, Any]:  # type: ignore[override]

        try:
            target = Path(os.path.expanduser(path))

            if not target.exists():
                return {"success": False, "error": f"Path does not exist: {path}"}

            if _is_path_protected(target):
                return {"success": False, "error": f"Refusing to delete protected path: {path}"}
            scope_err = _enforce_project_scope(target, "delete")
            if scope_err:
                return scope_err
            # Refuse a symlink leaf. ``Path.unlink`` removes the link itself
            # (safe), but ``shutil.rmtree`` on a symlinked directory can walk
            # into the link target on some platforms — and either way we'd
            # rather not delete-via-symlink at all when the link could have
            # been swapped between the protection check and now.
            symlink_err = _reject_symlink_leaf(target, "delete")
            if symlink_err:
                return symlink_err

            # Backup file before deletion for undo support
            if target.is_file():
                await asyncio.to_thread(backup_store.backup_file, str(target), "delete")

            def _delete():
                # Re-check symlink right before deletion to guard against
                # a TOCTOU swap between the lstat check and the unlink/rmtree.
                symlink_err2 = _reject_symlink_leaf(target, "delete")
                if symlink_err2:
                    raise OSError("Path was replaced by a symlink after validation")
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
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


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
    category = "filesystem"
    parameters_model = CreateDirectoryParams
    requires_confirmation = True

    async def execute(self, path: str, parents: bool = True) -> dict[str, Any]:  # type: ignore[override]
        try:
            target = Path(os.path.expanduser(path))

            if _is_path_protected(target):
                return {
                    "success": False,
                    "error": f"Refusing to create directory in protected path: {path}",
                }
            scope_err = _enforce_project_scope(target, "create_directory")
            if scope_err:
                return scope_err

            def _mkdir():
                target.mkdir(parents=parents, exist_ok=True)

            await asyncio.to_thread(_mkdir)
            return {
                "success": True,
                "path": str(target.resolve()),
                "message": f"Directory created: '{target}'",
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }
