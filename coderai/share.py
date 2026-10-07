"""Shared directory resolution.

``CODERAI_SHARE_DIR`` overrides; otherwise ``~/.coderai``. The directory is
created on demand so log/config writers never have to ensure it themselves.
"""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path


def get_share_dir() -> Path:
    """Get the share directory path."""
    if share_dir := os.getenv("CODERAI_SHARE_DIR"):
        path = Path(share_dir).expanduser()
    else:
        path = Path.home() / ".coderai"
    path.mkdir(parents=True, exist_ok=True)
    return path


def secure_share_subdir(name: str) -> Path:
    """Return ``get_share_dir() / name`` created with ``0700`` permissions.

    Single home for the mkdir+chmod hardening so token stores cannot silently
    diverge. Callers keep distinct subdirectory names (never merge stores).
    """
    path = get_share_dir() / name
    path.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        path.chmod(0o700)
    return path
