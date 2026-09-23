"""Atomic JSON persistence.

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


def atomic_write_text(
    path: Path | str,
    content: str,
    encoding: str = "utf-8",
    errors: str = "strict",
) -> int:
    """Write text data to a file atomically using a temporary file and os.replace."""
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with open(fd, "w", encoding=encoding, errors=errors) as f:
            chars_written = f.write(content)
            f.flush()
            with contextlib.suppress(OSError):
                os.fsync(f.fileno())
        os.replace(tmp_path, target)
        return chars_written
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


async def async_atomic_write_text(
    path: Path | str,
    content: str,
    encoding: str = "utf-8",
    errors: str = "strict",
) -> int:
    """Async wrapper around atomic_write_text."""
    import asyncio

    return await asyncio.to_thread(atomic_write_text, path, content, encoding, errors)

