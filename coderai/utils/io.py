"""Atomic JSON persistence.

Tmp-file + ``os.replace`` + ``fsync`` so a crash mid-write keeps either the
old file intact or the new file fully committed — never a torn file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def atomic_json_write(data: Any, path: Path | str, mode: int | None = None) -> None:
    """Write JSON data to a file atomically using tmp-file + os.replace, preserving file mode."""
    content = json.dumps(data, indent=2, ensure_ascii=False)
    atomic_write_text(path, content, encoding="utf-8", mode=mode)


def atomic_write_text(
    path: Path | str,
    content: str,
    encoding: str = "utf-8",
    errors: str = "strict",
    mode: int | None = None,
) -> int:
    """Write text data to a file atomically, preserving existing mode bits."""
    from coderai.utils.path import write_file_atomic

    enc = "utf16le" if encoding.lower().replace("-", "") == "utf16le" else "utf8"
    return write_file_atomic(path, content, mode=mode, encoding=enc)


async def async_atomic_write_text(
    path: Path | str,
    content: str,
    encoding: str = "utf-8",
    errors: str = "strict",
    mode: int | None = None,
) -> int:
    """Async wrapper around atomic_write_text."""
    import asyncio

    return await asyncio.to_thread(
        atomic_write_text,
        path,
        content,
        encoding=encoding,
        errors=errors,
        mode=mode,
    )
