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

import os
import stat

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
