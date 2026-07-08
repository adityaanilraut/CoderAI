"""Cross-platform helpers for restricting file permissions to the owner.

On POSIX we ``chmod``/``fchmod`` files that hold secrets — API keys, OAuth
tokens, and session history — so only the owner can read them. Windows has
neither ``os.fchmod`` (the attribute is absent, so calling it raises
``AttributeError``) nor POSIX permission bits, so these helpers degrade to a
no-op there and rely on the default per-user ACLs of the ``%USERPROFILE%``
profile directory where ``~/.coderAI`` lives.

Using these helpers instead of calling ``os.fchmod``/``os.chmod`` directly
keeps the atomic-write paths (config, history, MCP servers/credentials)
working on Windows instead of crashing mid-save.
"""

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Optional

# 0o600 — owner read/write only (files).
OWNER_RW = stat.S_IRUSR | stat.S_IWUSR
# 0o700 — owner read/write/execute only (directories).
OWNER_RWX = stat.S_IRWXU


def restrict_fd(fd: int, mode: int = OWNER_RW) -> None:
    """Restrict an open file descriptor to *mode*.

    No-op on platforms without ``os.fchmod`` (Windows). Permission hardening
    is best-effort: an ``OSError`` here must never abort the surrounding write.
    """
    fchmod = getattr(os, "fchmod", None)
    if fchmod is None:
        return
    try:
        fchmod(fd, mode)
    except OSError:
        pass


def restrict_path(path: "os.PathLike[str] | str", mode: int = OWNER_RW) -> None:
    """Restrict a filesystem path to *mode*.

    No-op on Windows, where ``os.chmod`` only toggles the read-only bit and
    carries no POSIX semantics. Best-effort: failures are swallowed.
    """
    if os.name == "nt":
        return
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def atomic_write_text(
    path: "os.PathLike[str] | str",
    text: str,
    *,
    mode: Optional[int] = OWNER_RW,
    fsync: bool = False,
    encoding: str = "utf-8",
) -> None:
    """Atomically write *text* to *path* via ``mkstemp`` + ``os.replace``.

    Writes to a temp file in the same directory and atomically renames it into
    place, so a crash mid-write can never leave a truncated file and a
    concurrent reader never sees a partial one. On any error the temp file is
    removed and the exception re-raised — the caller decides how to surface it.
    The parent directory must already exist.

    ``mode`` (default ``0o600``) restricts the temp file to its owner *before*
    the rename, so a file holding secrets or conversation content is never
    briefly world-readable; pass ``None`` to inherit the process umask (e.g. for
    user project files that should keep their existing permissions). Set
    ``fsync=True`` to force the bytes to disk before the rename for
    durability-critical stores.
    """
    p = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    try:
        if mode is not None:
            restrict_fd(fd, mode)
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp, str(p))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    finally:
        # ``fdopen`` takes ownership of ``fd`` and closes it; this double-close
        # only matters when ``fdopen`` itself raised before taking ownership.
        try:
            os.close(fd)
        except OSError:
            pass


def atomic_write_json(
    path: "os.PathLike[str] | str",
    obj: Any,
    *,
    mode: Optional[int] = OWNER_RW,
    fsync: bool = False,
    indent: Optional[int] = 2,
) -> None:
    """Atomically write *obj* as JSON to *path*.

    Thin JSON wrapper over :func:`atomic_write_text` — see it for the atomicity,
    permission (``mode``) and durability (``fsync``) semantics. ``indent``
    defaults to pretty-printing (2); pass ``None`` for compact output.
    """
    atomic_write_text(path, json.dumps(obj, indent=indent), mode=mode, fsync=fsync)
