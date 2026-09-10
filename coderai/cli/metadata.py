"""Workdir → last-session metadata (Kimi ``metadata.py`` parity, stdlib only).

Single JSON file at ``~/.coderai/coderai.json`` (Kimi: ``~/.kimi/kimi.json``)
mapping each resolved workdir path to its most recent session id. Written
best-effort on session create/fork; read for ``--continue`` with no index
hit. Legacy ``pydantic``/``kaos`` coupling intentionally dropped.
"""

from __future__ import annotations

import json
import pathlib
import time
from typing import Any


def get_metadata_file() -> pathlib.Path:
    """Return the metadata file path (``~/.coderai/coderai.json``)."""
    from coderai.core.share import get_share_dir

    return get_share_dir() / "coderai.json"


def _read() -> dict[str, Any]:
    path = get_metadata_file()
    try:
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write(data: dict[str, Any]) -> None:
    path = get_metadata_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def _norm_workdir(workdir: str) -> str:
    try:
        return str(pathlib.Path(workdir).expanduser().resolve())
    except Exception:
        return workdir


def load_metadata() -> dict[str, Any]:
    """Load raw metadata mapping (tolerates missing/corrupt files)."""
    return _read()


def save_metadata(data: dict[str, Any]) -> None:
    """Persist raw metadata mapping (best-effort, never raises)."""
    if isinstance(data, dict):
        _write(data)


def record_last_session(workdir: str, session_id: str) -> None:
    """Remember ``session_id`` as the latest session for ``workdir``."""
    if not session_id:
        return
    data = _read()
    dirs = data.get("work_dirs")
    if not isinstance(dirs, dict):
        dirs = {}
        data["work_dirs"] = dirs
    dirs[_norm_workdir(workdir)] = {
        "last_session_id": session_id,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    _write(data)


def get_last_session_id(workdir: str) -> str | None:
    """Return the last session id recorded for ``workdir``, if any."""
    dirs = _read().get("work_dirs")
    if not isinstance(dirs, dict):
        return None
    entry = dirs.get(_norm_workdir(workdir))
    if isinstance(entry, dict):
        sid = entry.get("last_session_id")
        return str(sid) if sid else None
    if isinstance(entry, str):
        return entry or None
    return None
