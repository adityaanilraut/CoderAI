"""Shared path resolution for project-local CoderAI directories."""

from pathlib import Path
from typing import Optional


def find_dot_coderai_subdir(
    relative_under_dot_coderai: str,
    project_root: str = ".",
) -> Optional[Path]:
    """Locate ``.coderAI/<relative_under_dot_coderai>`` if it exists as a directory.

    Checks, in order: the given ``project_root``, the current working directory,
    and the repository/package root next to this package (for development installs).
    """
    tail = Path(".coderAI") / relative_under_dot_coderai
    candidates = [
        Path(project_root).resolve(),
        Path.cwd(),
        Path(__file__).resolve().parent.parent,
    ]
    seen: set[str] = set()
    for base in candidates:
        key = str(base)
        if key in seen:
            continue
        seen.add(key)
        path = base / tail
        if path.is_dir():
            return path
    return None
