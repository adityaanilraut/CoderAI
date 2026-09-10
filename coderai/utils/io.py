# Ported from coderai/core/common/atomic.py - kimi structure (kimi_cli/utils/io.py).
"""Atomic JSON persistence (Kimi ``utils/io.py`` parity).

Tmp-file + ``os.replace`` + ``fsync`` so a crash mid-write keeps either the
old file intact or the new file fully committed — never a torn file.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_json_write(data: Any, path: Path) -> None:
    """Write JSON data to a file atomically using tmp-file + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            with contextlib.suppress(OSError):
                os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
