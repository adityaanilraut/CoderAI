"""Filesystem metadata tools: stat, chmod, chown, readlink."""

import os
import sys
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from coderAI.core.tool_error_codes import ToolErrorCode
from coderAI.tools.base import Tool

from coderAI.tools.filesystem._guards import (
    _O_NOFOLLOW,
    _enforce_project_scope,
    _is_path_protected,
    _reject_symlink_leaf,
)


class FileStatParams(BaseModel):
    path: str = Field(..., description="Path to the file or directory")


class FileStatTool(Tool):
    """Get detailed metadata about a file or directory."""

    name = "file_stat"
    description = "Get detailed file metadata: size, permissions, timestamps, type, owner"
    category = "filesystem"
    parameters_model = FileStatParams
    is_read_only = True

    async def execute(self, path: str) -> dict[str, Any]:  # type: ignore[override]
        try:
            target = Path(path).expanduser()
            scope_err = _enforce_project_scope(target, "stat")
            if scope_err:
                return scope_err
            if not target.exists():
                return {"success": False, "error": f"Path does not exist: {path}"}
            stat = target.stat()
            return {
                "success": True,
                "path": str(target.resolve()),
                "size": stat.st_size,
                "mode_octal": oct(stat.st_mode)[-4:],
                "mode": stat.st_mode,
                "mtime": stat.st_mtime,
                "atime": stat.st_atime,
                "ctime": stat.st_ctime,
                "uid": stat.st_uid,
                "gid": stat.st_gid,
                "is_file": target.is_file(),
                "is_dir": target.is_dir(),
                "is_symlink": target.is_symlink(),
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


class FileChmodParams(BaseModel):
    path: str = Field(..., description="Path to the file or directory")
    mode: str = Field(..., description="Octal mode string, e.g. '755' or '644'")


class FileChmodTool(Tool):
    """Change file permissions."""

    name = "file_chmod"
    description = "Change file or directory permissions using octal mode (e.g. '755', '644')"
    category = "filesystem"
    parameters_model = FileChmodParams
    requires_confirmation = True

    async def execute(self, path: str, mode: str) -> dict[str, Any]:  # type: ignore[override]
        try:
            target = Path(path).expanduser()
            if not target.exists():
                return {"success": False, "error": f"Path does not exist: {path}"}
            if _is_path_protected(target):
                return {"success": False, "error": f"Refusing to chmod protected path: {path}"}
            scope_err = _enforce_project_scope(target, "chmod")
            if scope_err:
                return scope_err
            symlink_err = _reject_symlink_leaf(target, "chmod")
            if symlink_err:
                return symlink_err
            mode_int = int(mode, 8)
            if sys.platform == "win32":
                # No O_NOFOLLOW on Windows; the lstat-based leaf check above is
                # the guard (same rationale as _guards.py's _safe_open_no_symlink).
                target.chmod(mode_int)
            else:
                # fd-based no-follow chmod: a swap-to-symlink in the TOCTOU gap
                # after the leaf check fails the open with ELOOP instead of
                # silently following the link.
                fd = os.open(str(target), os.O_RDONLY | _O_NOFOLLOW)
                try:
                    os.fchmod(fd, mode_int)
                finally:
                    os.close(fd)
            return {"success": True, "path": str(target.resolve()), "mode": mode}
        except PermissionError:
            return {"success": False, "error": f"Permission denied: {path}"}
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


class FileChownParams(BaseModel):
    path: str = Field(..., description="Path to the file or directory")
    owner: Optional[str] = Field(None, description="New owner name or UID")
    group: Optional[str] = Field(None, description="New group name or GID")


class FileChownTool(Tool):
    """Change file ownership (requires appropriate privileges)."""

    name = "file_chown"
    description = (
        "Change file or directory owner and/or group. Typically requires root/sudo. POSIX only."
    )
    category = "filesystem"
    parameters_model = FileChownParams
    requires_confirmation = True

    async def execute(  # type: ignore[override]
        self, path: str, owner: Optional[str] = None, group: Optional[str] = None
    ) -> dict[str, Any]:
        if sys.platform == "win32":
            return {
                "success": False,
                "error": "file_chown is not supported on Windows.",
                "error_code": ToolErrorCode.UNSUPPORTED_PLATFORM,
            }
        try:
            if not owner and not group:
                return {
                    "success": False,
                    "error": "At least one of 'owner' or 'group' must be specified",
                }
            target = Path(path).expanduser()
            if not target.exists():
                return {"success": False, "error": f"Path does not exist: {path}"}
            if _is_path_protected(target):
                return {"success": False, "error": f"Refusing to chown protected path: {path}"}
            scope_err = _enforce_project_scope(target, "chown")
            if scope_err:
                return scope_err
            symlink_err = _reject_symlink_leaf(target, "chown")
            if symlink_err:
                return symlink_err
            uid = int(owner) if (owner and owner.isdigit()) else -1
            gid = int(group) if (group and group.isdigit()) else -1
            import pwd
            import grp

            if owner and uid == -1:
                uid = pwd.getpwnam(owner).pw_uid
            if group and gid == -1:
                gid = grp.getgrnam(group).gr_gid
            # fd-based no-follow chown (POSIX-only tool): closes the same
            # symlink-TOCTOU gap that os.chown(target.resolve()) leaves open.
            fd = os.open(str(target), os.O_RDONLY | _O_NOFOLLOW)
            try:
                os.fchown(fd, uid if owner else -1, gid if group else -1)
            finally:
                os.close(fd)
            return {"success": True, "path": str(target.resolve()), "owner": owner, "group": group}
        except PermissionError:
            return {"success": False, "error": f"Permission denied changing ownership of: {path}"}
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


class FileReadlinkParams(BaseModel):
    path: str = Field(..., description="Path to the symlink")


class FileReadlinkTool(Tool):
    """Read the target of a symbolic link."""

    name = "file_readlink"
    description = "Read the target path of a symbolic link"
    category = "filesystem"
    parameters_model = FileReadlinkParams
    is_read_only = True

    async def execute(self, path: str) -> dict[str, Any]:  # type: ignore[override]
        try:
            target = Path(path).expanduser()
            scope_err = _enforce_project_scope(target, "readlink")
            if scope_err:
                return scope_err
            # Check is_symlink() (lstat-based) before exists() (follows the
            # link): a broken symlink has exists()==False but is a valid
            # readlink target, so testing exists() first misreports it as
            # "Path does not exist".
            if not target.is_symlink():
                if not target.exists():
                    return {"success": False, "error": f"Path does not exist: {path}"}
                return {"success": False, "error": f"Not a symlink: {path}"}
            resolved = target.readlink()
            return {"success": True, "path": str(target.resolve()), "target": str(resolved)}
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }
