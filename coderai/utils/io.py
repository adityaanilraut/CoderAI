"""Atomic JSON persistence.

Tmp-file + ``os.replace`` + ``fsync`` so a crash mid-write keeps either the
old file intact or the new file fully committed — never a torn file.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def atomic_json_write(data: Any, path: Path | str, mode: int | None = None) -> None:
    """Write JSON data to a file atomically using tmp-file + os.replace, preserving file mode."""
    content = json.dumps(data, indent=2, ensure_ascii=False)
    atomic_write_text(path, content, encoding="utf-8", mode=mode)


def atomic_write_text(
    path: Path | str,
    content: str | Iterable[str],
    encoding: str = "utf-8",
    errors: str = "strict",
    mode: int | None = None,
    *,
    follow_symlinks: bool = True,
) -> int:
    """Atomically write text or streamed chunks, preserving or setting mode bits.

    Set follow_symlinks=False to replace a link instead of its destination.
    """
    from coderai.utils.path import write_file_atomic

    return write_file_atomic(
        path,
        content,
        mode=mode,
        encoding=encoding,
        errors=errors,
        follow_symlinks=follow_symlinks,
    )


async def async_atomic_write_text(
    path: Path | str,
    content: str | Iterable[str],
    encoding: str = "utf-8",
    errors: str = "strict",
    mode: int | None = None,
    *,
    follow_symlinks: bool = True,
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
        follow_symlinks=follow_symlinks,
    )
