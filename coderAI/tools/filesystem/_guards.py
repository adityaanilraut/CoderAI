"""Security core for filesystem tools.

Path protection, project-scope enforcement, symlink guards, atomic writes,
and size limits. This surface is pinned by ``tests/test_path_traversal.py``
and ``tests/test_filesystem_symlink_guard.py`` — behavior changes here are
security-relevant.
"""

import difflib
import logging
import os
import sys
from pathlib import Path
from typing import IO, Any, Optional, Union

from coderAI.core.services import get_services
from coderAI.types.tool_error_codes import ToolErrorCode
from coderAI.system.events import event_emitter
from coderAI.system.fsperms import atomic_write_text

logger = logging.getLogger(__name__)


def _atomic_write_file(path_obj: Path, content: str) -> None:
    """Write *content* to *path_obj* atomically via tempfile+replace.

    Uses ``mode=None`` so the written file keeps the process umask — these are
    the user's own project files, which must not be forced down to owner-only
    like the ``.coderAI`` metadata stores are. Raises ``OSError`` on write
    failure; the caller translates it into a tool-shaped error response.
    """
    atomic_write_text(path_obj, content, mode=None)


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


class ProjectPathError(ValueError):
    """A path failed a project filesystem security policy."""

    def __init__(self, message: str, error_code: ToolErrorCode) -> None:
        super().__init__(message)
        self.error_code = error_code

    def as_result(self) -> dict[str, Any]:
        return {
            "success": False,
            "error": str(self),
            "error_code": self.error_code,
        }


def _get_max_file_size() -> int:
    """Get max file size from config."""
    try:
        return get_services().config.max_file_size
    except Exception:
        # Config unavailable (corrupt file, tests) → built-in default.
        logger.debug("max_file_size config unavailable, using default", exc_info=True)
        return DEFAULT_MAX_FILE_SIZE


def _get_max_glob_results() -> int:
    """Get max glob results from config."""
    try:
        return get_services().config.max_glob_results
    except Exception:
        # Config unavailable (corrupt file, tests) → built-in default.
        logger.debug("max_glob_results config unavailable, using default", exc_info=True)
        return DEFAULT_MAX_GLOB_RESULTS


# Paths under $HOME that tools should never write to. These are checked *before*
# the ``allow_outside_project`` opt-out in every mutating filesystem tool, so they
# stay protected even when the project-scope sandbox is disabled. ``.coderAI`` is
# included so a mutating tool cannot rewrite CoderAI's own config / trust store /
# OAuth credentials (its internal machinery bypasses these tools).
PROTECTED_HOME_PATHS = [
    ".ssh",
    ".gnupg",
    ".aws",
    ".config",  # gcloud creds + many other apps' secret stores live here
    ".kube",
    ".docker",
    ".bash_history",
    ".zsh_history",
    # Shell/tool init files (a persistence vector) and credential stores.
    ".bashrc",
    ".zshrc",
    ".profile",
    ".bash_profile",
    ".gitconfig",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".coderAI",
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
    "/System",  # macOS system dir
    "/Library",  # macOS shared library dir
    "/private/etc",
    "/var/log",
    "/root",
]

if sys.platform == "win32":
    PROTECTED_SYSTEM_PATHS.extend(
        [
            "C:\\Windows",
            "C:\\Windows\\System32",
            "C:\\Program Files",
            "C:\\Program Files (x86)",
        ]
    )


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


def _is_in_project_root(path: Path) -> bool:
    """Return True if *path* resolves underneath the configured project root.

    ``.resolve()`` follows every symlink in the chain, so a symlinked ancestor
    that points at ``/etc`` cannot be used to dodge this check.
    """
    try:
        cfg = get_services().config
        project_root = Path(getattr(cfg, "project_root", ".") or ".").resolve()
    except Exception:
        # Config unavailable → treat the current directory as the project
        # root; the scope check below still runs against it.
        logger.debug("project_root config unavailable, using cwd", exc_info=True)
        project_root = Path.cwd().resolve()
    try:
        path.resolve().relative_to(project_root)
        return True
    except (ValueError, OSError):
        return False


def _allows_outside_project() -> bool:
    """True when config or ``CODERAI_ALLOW_OUTSIDE_PROJECT=1`` opts out of scope."""
    if os.environ.get("CODERAI_ALLOW_OUTSIDE_PROJECT") == "1":
        return True
    try:
        return bool(getattr(get_services().config, "allow_outside_project", False))
    except Exception:
        # Fail closed: if config can't be read, keep project-scope enforcement on.
        logger.debug("allow_outside_project config unavailable, failing closed", exc_info=True)
        return False


def resolve_under_project(
    path: Union[str, os.PathLike[str]],
    *,
    operation: str = "access",
    enforce_scope: bool = True,
    check_protected: bool = False,
    reject_symlink: bool = False,
) -> Path:
    """Return a canonical path resolved against the active project root.

    Relative paths are interpreted relative to ``get_services().config.project_root``,
    not the process working directory. Scope enforcement follows the existing
    ``allow_outside_project`` escape hatch. Optional protected-path and symlink-leaf
    checks let non-filesystem tools share the same mutation policy.
    """
    try:
        cfg = get_services().config
        project_root = Path(getattr(cfg, "project_root", ".") or ".").expanduser().resolve()
    except Exception:
        logger.debug("project_root config unavailable, using cwd", exc_info=True)
        project_root = Path.cwd().resolve()

    requested = Path(path).expanduser()
    candidate = requested if requested.is_absolute() else project_root / requested

    if reject_symlink:
        try:
            if candidate.is_symlink():
                raise ProjectPathError(
                    f"Refusing to {operation} a symlink leaf: {candidate}",
                    ToolErrorCode.SYMLINK,
                )
        except OSError as exc:
            raise ProjectPathError(
                f"Unable to validate path for {operation}: {candidate}: {exc}",
                ToolErrorCode.IO,
            ) from exc

    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise ProjectPathError(
            f"Unable to resolve path for {operation}: {candidate}: {exc}",
            ToolErrorCode.IO,
        ) from exc

    if check_protected and _is_path_protected(resolved):
        raise ProjectPathError(
            f"Refusing to {operation} protected path: {resolved}",
            ToolErrorCode.PERMISSION_DENIED,
        )

    if enforce_scope:
        try:
            resolved.relative_to(project_root)
        except ValueError:
            if not _allows_outside_project():
                raise ProjectPathError(
                    (
                        f"Refusing to {operation} outside project root: {resolved}. "
                        "Set CODERAI_ALLOW_OUTSIDE_PROJECT=1 to allow."
                    ),
                    ToolErrorCode.SCOPE,
                ) from None
            logger.warning("%s outside project root: %s (allowed by opt-out)", operation, resolved)

    return resolved


def _enforce_project_scope(path: Path, op: str) -> Optional[dict[str, Any]]:
    """Reject file operations outside the project root.

    Set ``CODERAI_ALLOW_OUTSIDE_PROJECT=1`` to opt out (e.g. when editing
    dotfiles or scratch files outside the repo).
    """
    if _is_in_project_root(path):
        return None
    if _allows_outside_project():
        logger.warning("%s outside project root: %s (allowed by opt-out)", op, path)
        return None
    return {
        "success": False,
        "error": (
            f"Refusing to {op} outside project root: {path}. "
            "Set CODERAI_ALLOW_OUTSIDE_PROJECT=1 to allow."
        ),
        "error_code": ToolErrorCode.SCOPE,
    }


def _reject_symlink_leaf(path: Path, op: str) -> Optional[dict[str, Any]]:
    """Refuse to operate on a symlink leaf.

    Mitigates the symlink-TOCTOU pattern: ``_is_path_protected`` and
    ``_enforce_project_scope`` both call ``Path.resolve()``, which follows
    every symlink in the chain — so a path that *currently* points at a
    benign file inside the project passes, even though the link could be
    swapped to point at ``/etc/passwd`` between the check and the
    subsequent open. Refusing symlink leaves outright closes the common
    attack shape; ``_safe_open_no_symlink`` below is the second layer
    against a swap inside the microsecond gap between the lstat and the
    actual ``open()``.
    """
    try:
        if path.is_symlink():
            return {
                "success": False,
                "error": (
                    f"Refusing to {op} a symlink leaf: {path}. "
                    "Operate on the resolved target path directly."
                ),
                "error_code": ToolErrorCode.SYMLINK,
            }
    except OSError:
        # ``is_symlink`` raises on broken paths; let the caller's exists()
        # check produce the canonical error.
        pass
    return None


# ``O_NOFOLLOW`` is POSIX-only; on Windows the constant is absent and the
# open flag has no equivalent (Windows does not have user-space symlinks
# in the same shape). Falling back to 0 is acceptable because the lstat
# check above is the primary guard; O_NOFOLLOW is the belt-and-suspenders.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _safe_open_no_symlink(path: Path, mode: str, encoding: Optional[str] = "utf-8") -> IO[str]:
    """Open *path* with ``O_NOFOLLOW`` so a swap-to-symlink between
    ``_reject_symlink_leaf`` and the open will fail with ``OSError(ELOOP)``
    instead of silently following the link.

    ``mode`` is the standard textual mode (``"r"``, ``"w"``, ``"a"``).
    Translates to the matching POSIX flags. The caller is responsible for
    converting ``OSError`` into a tool-shaped error response.
    """
    if mode == "r":
        flags = os.O_RDONLY
    elif mode == "w":
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    elif mode == "a":
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    else:
        raise ValueError(f"Unsupported mode for safe open: {mode!r}")
    flags |= _O_NOFOLLOW
    fd = os.open(str(path), flags, 0o666)
    return os.fdopen(fd, mode, encoding=encoding)
