"""Directory browsing and file management tools: list, glob, move, copy, delete, mkdir."""

import asyncio
import shutil as _shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from coderAI.system.constants import SKIP_DIRS
from coderAI.system.proc import run_scrubbed, subprocess_timeout
from coderAI.types.tool_error_codes import ToolErrorCode
from coderAI.tools.base import Tool
from coderAI.tools.undo import get_backup_store

from coderAI.tools.filesystem._guards import (
    _enforce_project_scope,
    _get_max_glob_results,
    _is_path_protected,
    _reject_symlink_leaf,
    ProjectPathError,
    resolve_under_project,
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
            path_obj = resolve_under_project(path, operation="list")
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
        except ProjectPathError as e:
            return e.as_result()
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
            base = resolve_under_project(base_path, operation="glob_search")
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
            was_truncated = False
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

                    if len(matches) >= max_glob_results:
                        was_truncated = True
                        break
                    matches.append(
                        str(match.relative_to(base) if match.is_relative_to(base) else match)
                    )

            result = {
                "success": True,
                "pattern": pattern,
                "matches": matches,
                "count": len(matches),
                "was_truncated": was_truncated,
            }

            if was_truncated:
                result["note"] = (
                    f"Results capped at {max_glob_results}; more matches exist. "
                    "Use a more specific pattern to narrow results."
                )

            return result
        except ProjectPathError as e:
            return e.as_result()
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


def _validate_transfer(
    source: str, destination: str, overwrite: bool, verb: str
) -> tuple[Path, Path, dict[str, Any] | None]:
    """Shared security preamble for move/copy: protection, project-scope,
    symlink-leaf and overwrite checks on both endpoints.

    Returns ``(src, dst, error_or_None)``. Security-sensitive — MoveFileTool
    and CopyFileTool must not drift apart; both also re-check symlink leaves
    right before mutating via :func:`_recheck_symlinks`.
    """
    src = resolve_under_project(
        source,
        operation=f"{verb} from",
        check_protected=True,
        reject_symlink=True,
    )
    dst = resolve_under_project(
        destination,
        operation=f"{verb} to",
        check_protected=True,
        reject_symlink=True,
    )

    if not src.exists():
        return src, dst, {"success": False, "error": f"Source does not exist: {source}"}

    if _is_path_protected(src):
        return src, dst, {"success": False, "error": f"Source is in a protected path: {source}"}
    scope_err = _enforce_project_scope(src, "move/copy")
    if scope_err:
        return src, dst, scope_err
    if _is_path_protected(dst):
        return (
            src,
            dst,
            {"success": False, "error": f"Destination is in a protected path: {destination}"},
        )
    scope_err = _enforce_project_scope(dst, "move/copy")
    if scope_err:
        return src, dst, scope_err
    # Refuse symlink leaves on either side. ``_is_path_protected`` resolves
    # through symlinks (and ``shutil.copy2`` follows them and copies the
    # *target's* contents), so a swap between check and operation could
    # redirect onto a protected target or pull ``/etc/passwd`` into the project.
    symlink_err = _reject_symlink_leaf(src, f"{verb} from") or _reject_symlink_leaf(
        dst, f"{verb} to"
    )
    if symlink_err:
        return src, dst, symlink_err

    if dst.exists() and not overwrite:
        return (
            src,
            dst,
            {
                "success": False,
                "error": f"Destination already exists: {destination}. Set overwrite=true to replace it.",
            },
        )
    return src, dst, None


def _recheck_symlinks(src: Path, dst: Path, verb: str) -> None:
    """Re-check both leaves right before the operation to guard against a
    TOCTOU swap after validation (mirrors DeleteFileTool._delete)."""
    if _reject_symlink_leaf(src, f"{verb} from") or _reject_symlink_leaf(dst, f"{verb} to"):
        raise OSError("Path was replaced by a symlink after validation")


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
            src, dst, error = _validate_transfer(source, destination, overwrite, "move")
            if error:
                return error

            dst.parent.mkdir(parents=True, exist_ok=True)

            # Backup source (it will be removed) and destination (if overwritten)
            if src.is_file():
                await asyncio.to_thread(get_backup_store().backup_file, str(src), "delete")
            if dst.exists() and dst.is_file():
                await asyncio.to_thread(get_backup_store().backup_file, str(dst), "modify")

            def _move():
                _recheck_symlinks(src, dst, "move")
                _shutil.move(str(src), str(dst))

            await asyncio.to_thread(_move)
            return {
                "success": True,
                "source": str(src),
                "destination": str(dst),
                "message": f"Moved '{src}' → '{dst}'",
            }
        except ProjectPathError as e:
            return e.as_result()
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
            src, dst, error = _validate_transfer(source, destination, overwrite, "copy")
            if error:
                return error

            dst.parent.mkdir(parents=True, exist_ok=True)

            # Backup destination if it will be overwritten
            if dst.exists() and dst.is_file():
                await asyncio.to_thread(get_backup_store().backup_file, str(dst), "modify")

            def _copy():
                _recheck_symlinks(src, dst, "copy")
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
        except ProjectPathError as e:
            return e.as_result()
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
            target = resolve_under_project(
                path,
                operation="delete",
                check_protected=True,
                reject_symlink=True,
            )

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
                await asyncio.to_thread(get_backup_store().backup_file, str(target), "delete")

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
        except ProjectPathError as e:
            return e.as_result()
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
            target = resolve_under_project(
                path,
                operation="create_directory",
                check_protected=True,
                reject_symlink=True,
            )

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
        except ProjectPathError as e:
            return e.as_result()
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


class DirectoryTreeParams(BaseModel):
    path: str = Field(
        ".", description="Path to the directory to render (default: current directory)"
    )
    max_depth: int = Field(
        3, ge=1, le=10, description="Maximum directory depth to traverse (default: 3)"
    )
    include_hidden: bool = Field(
        False, description="Whether to include hidden files and dot-directories (default: false)"
    )


class DirectoryTreeTool(Tool):
    """Tool for displaying a visual tree structure of a directory."""

    name = "directory_tree"
    description = (
        "Display a visual hierarchy/tree of files and subdirectories up to max_depth. "
        "Useful for getting a high-level overview of project structure."
    )
    category = "filesystem"
    parameters_model = DirectoryTreeParams
    is_read_only = True

    async def execute(  # type: ignore[override]
        self, path: str = ".", max_depth: int = 3, include_hidden: bool = False
    ) -> dict[str, Any]:
        try:
            target = resolve_under_project(path, operation="list")
            scope_err = _enforce_project_scope(target, "list")
            if scope_err:
                return scope_err

            if not target.exists():
                return {
                    "success": False,
                    "error": f"Directory not found: {path}",
                    "error_code": ToolErrorCode.NOT_FOUND,
                }

            if not target.is_dir():
                return {
                    "success": False,
                    "error": f"Not a directory: {path}",
                    "error_code": ToolErrorCode.NOT_A_DIRECTORY,
                }

            def _generate_tree() -> tuple[list[str], int, int]:
                lines: list[str] = [target.name or str(target)]
                dir_count = 0
                file_count = 0
                skip_set = set(SKIP_DIRS)

                def _walk(current: Path, prefix: str, depth: int) -> None:
                    nonlocal dir_count, file_count
                    if depth > max_depth:
                        return

                    try:
                        raw_entries = sorted(
                            current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
                        )
                    except OSError:
                        return

                    entries = []
                    for e in raw_entries:
                        if not include_hidden and e.name.startswith("."):
                            continue
                        if e.name in skip_set:
                            continue
                        entries.append(e)

                    for idx, entry in enumerate(entries):
                        is_last = idx == len(entries) - 1
                        connector = "└── " if is_last else "├── "
                        extension = "    " if is_last else "│   "

                        if entry.is_dir():
                            dir_count += 1
                            lines.append(f"{prefix}{connector}{entry.name}/")
                            _walk(entry, prefix + extension, depth + 1)
                        else:
                            file_count += 1
                            lines.append(f"{prefix}{connector}{entry.name}")

                _walk(target, "", 1)
                return lines, dir_count, file_count

            lines, total_dirs, total_files = await asyncio.to_thread(_generate_tree)

            return {
                "success": True,
                "path": str(target.resolve()),
                "tree": "\n".join(lines),
                "total_directories": total_dirs,
                "total_files": total_files,
                "max_depth": max_depth,
            }
        except ProjectPathError as e:
            return e.as_result()
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


class WorkspaceStatusParams(BaseModel):
    path: str = Field(".", description="Project directory path (default: current directory)")
    include_recent_minutes: int = Field(
        15, ge=1, le=1440, description="Include files modified in the last N minutes (default: 15)"
    )


class WorkspaceStatusTool(Tool):
    """Inspect workspace state, active git changes, and recently modified files."""

    name = "workspace_status"
    description = (
        "Check workspace status: active git changes (modified, untracked, staged files, active branch) "
        "and list files recently modified by external editors or agents."
    )
    category = "filesystem"
    parameters_model = WorkspaceStatusParams
    is_read_only = True

    async def execute(  # type: ignore[override]
        self, path: str = ".", include_recent_minutes: int = 15
    ) -> dict[str, Any]:
        try:
            target = resolve_under_project(path, operation="list")
            scope_err = _enforce_project_scope(target, "list")
            if scope_err:
                return scope_err

            if not target.exists() or not target.is_dir():
                return {
                    "success": False,
                    "error": f"Directory not found: {path}",
                    "error_code": ToolErrorCode.NOT_FOUND,
                }

            import time

            cutoff = time.time() - (include_recent_minutes * 60)
            scan_cap = max(50, _get_max_glob_results())
            recent_files: list[dict[str, Any]] = []
            scanned = 0
            truncated_scan = False

            def _scan_recent() -> None:
                nonlocal scanned, truncated_scan
                skip_set = set(SKIP_DIRS)
                for item in target.rglob("*"):
                    scanned += 1
                    if scanned > scan_cap:
                        truncated_scan = True
                        return
                    if item.is_file() and not any(p in skip_set for p in item.parts):
                        try:
                            mtime = item.stat().st_mtime
                            if mtime >= cutoff:
                                recent_files.append(
                                    {
                                        "path": str(item.relative_to(target)),
                                        "modified_seconds_ago": int(time.time() - mtime),
                                        "size_bytes": item.stat().st_size,
                                    }
                                )
                        except OSError:
                            continue

            await asyncio.to_thread(_scan_recent)

            is_git = False
            branch = None
            git_changes: list[dict[str, str]] = []

            git_timeout = min(5.0, float(subprocess_timeout()))
            returncode, stdout, _stderr, timed_out = await run_scrubbed(
                ["git", "status", "--porcelain=v1", "-b"],
                cwd=str(target),
                shell=False,
                timeout=git_timeout,
            )
            if not timed_out and returncode == 0:
                is_git = True
                lines = stdout.decode("utf-8", errors="replace").splitlines()
                if lines and lines[0].startswith("## "):
                    branch = lines[0][3:].split("...")[0].strip()
                    lines = lines[1:]
                for line in lines:
                    if len(line) >= 3:
                        st = line[:2].strip()
                        fn = line[3:].strip()
                        git_changes.append({"status": st, "file": fn})

            return {
                "success": True,
                "path": str(target.resolve()),
                "is_git_repository": is_git,
                "active_branch": branch,
                "git_changed_files_count": len(git_changes),
                "git_changes": git_changes[:50],
                "recent_modified_files_count": len(recent_files),
                "recent_modified_files": sorted(
                    recent_files, key=lambda x: x["modified_seconds_ago"]
                )[:30],
                "scan_truncated": truncated_scan,
            }
        except ProjectPathError as e:
            return e.as_result()
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }
