"""Shared walk-up-and-probe detection loop for the code-quality tools.

format / lint / testing / package_manager all detect "which tool does this
project use" the same way: walk from the start path up to the first ``.git``
boundary, and in each directory look for per-tool indicator files, then check
the tool is actually usable. Only the availability check differs per tool
family, so it is injected as a callback.
"""

from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence


def walk_up_detect(
    project_root: str,
    table: Mapping[str, Mapping[str, Any]],
    order: Sequence[str],
    available: Callable[[str, Path], Optional[str]],
) -> Optional[str]:
    """Walk from *project_root* up to the first ``.git`` boundary probing for a tool.

    In each directory, tools are tried in *order*: when one of a tool's
    ``detect_files`` indicator files exists there, ``available(name, directory)``
    decides — returning the detected tool name, or ``None`` to keep looking.
    """
    start_path = Path(project_root).resolve()
    if start_path.is_file():
        start_path = start_path.parent

    for current_dir in [start_path] + list(start_path.parents):
        for name in order:
            if any((current_dir / f).exists() for f in table[name]["detect_files"]):
                hit = available(name, current_dir)
                if hit:
                    return hit
        if (current_dir / ".git").exists():
            break

    return None
