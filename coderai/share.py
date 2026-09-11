# Ported from coderai/core/share.py - kimi structure (share.py).
"""Shared directory resolution (Kimi ``share.py`` parity).

``CODERAI_SHARE_DIR`` overrides; otherwise ``~/.coderai``. The directory is
created on demand so log/config writers never have to ensure it themselves.
"""

from __future__ import annotations

import os
from pathlib import Path


def get_share_dir() -> Path:
    """Get the share directory path."""
    if share_dir := (os.getenv("CODERAI_SHARE_DIR") or os.getenv("KIMI_SHARE_DIR")):
        path = Path(share_dir).expanduser()
    else:
        path = Path.home() / ".coderai"
    path.mkdir(parents=True, exist_ok=True)
    return path
