"""Shared path resolution for project-local CoderAI directories."""

import json as _json
from pathlib import Path
from typing import Any, Dict, Optional


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


def read_current_plan(project_root: str = ".") -> Optional[Dict[str, Any]]:
    """Read ``current_plan.json`` from the project's ``.coderAI/`` directory.

    Returns the parsed plan dict, or ``None`` on any failure (missing file,
    malformed JSON, permission error).
    """
    try:
        dot_dir = find_dot_coderai_subdir("", str(project_root))
        if dot_dir is None:
            dot_dir = Path(project_root).resolve() / ".coderAI"
        plan_path = dot_dir / "current_plan.json"
        if not plan_path.exists():
            return None
        with open(plan_path, "r") as pf:
            return _json.load(pf)
    except Exception:
        return None
