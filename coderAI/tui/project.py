"""Project file scanning extracted from CoderAIApp."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import List

from coderAI.system.constants import SKIP_DIRS

SKIP_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".pdf",
        ".zip",
        ".tar",
        ".gz",
        ".7z",
        ".mp3",
        ".mp4",
        ".mov",
        ".pyc",
        ".pyo",
        ".so",
        ".dylib",
        ".dll",
        ".exe",
        ".bin",
        ".lock",
    }
)


def scan_project_files(root: str) -> List[str]:
    """Walk *root* and return a sorted list of relative file paths.

    Skips binary, media, lock, and build-artifact directories / extensions.
    """
    root_path = Path(root).resolve()
    files_list: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root_path):
        # Prune skipped directories in-place so os.walk doesn't descend into them.
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fname in filenames:
            file_path = Path(dirpath) / fname
            if file_path.suffix.lower() in SKIP_EXTENSIONS:
                continue
            try:
                rel_path = file_path.relative_to(root_path)
                files_list.append(str(rel_path))
            except ValueError:
                pass
    files_list.sort()
    return files_list


async def async_scan_project_files(root: str) -> List[str]:
    """Async wrapper for :func:`scan_project_files` using a thread pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, scan_project_files, root)
