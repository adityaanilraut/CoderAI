"""Work-directory metadata.

Single JSON file at ``<share_dir>/coderai.json`` holding
a ``work_dirs`` list of :class:`WorkDirMeta` records. This module is the *only*
implementation: it replaces the former ``coderai/cli/metadata.py``, which wrote
the same file in an incompatible ``{path: {...}}`` mapping shape and silently
discarded (or was discarded by) this one.
"""

from __future__ import annotations

import json
from hashlib import md5
from pathlib import Path
from typing import Any

from kaos import get_current_kaos
from kaos.local import local_kaos
from kaos.path import KaosPath
from pydantic import BaseModel, ConfigDict, Field

from coderai.share import get_share_dir
from coderai.utils.io import atomic_json_write
from coderai.utils.logging import logger


def get_metadata_file() -> Path:
    return get_share_dir() / "coderai.json"


class WorkDirMeta(BaseModel):
    """Metadata for a work directory."""

    path: str
    """The full path of the work directory."""

    kaos: str = local_kaos.name
    """The name of the KAOS where the work directory is located."""

    last_session_id: str | None = None
    """Last session ID of this work directory."""

    @property
    def sessions_dir(self) -> Path:
        """The directory to store sessions for this work directory."""
        path_md5 = md5(self.path.encode(encoding="utf-8")).hexdigest()
        dir_basename = path_md5 if self.kaos == local_kaos.name else f"{self.kaos}_{path_md5}"
        session_dir = get_share_dir() / "sessions" / dir_basename
        session_dir.mkdir(parents=True, exist_ok=True)
        return session_dir


class Metadata(BaseModel):
    """CoderAI metadata structure."""

    model_config = ConfigDict(extra="ignore")

    work_dirs: list[WorkDirMeta] = Field(default_factory=list)
    """Work directory list."""

    def get_work_dir_meta(self, path: KaosPath | str) -> WorkDirMeta | None:
        """Get the metadata for a work directory."""
        for wd in self.work_dirs:
            if wd.path == str(path) and wd.kaos == get_current_kaos().name:
                return wd
        return None

    def new_work_dir_meta(self, path: KaosPath | str) -> WorkDirMeta:
        """Create a new work directory metadata."""
        wd_meta = WorkDirMeta(path=str(path), kaos=get_current_kaos().name)
        self.work_dirs.append(wd_meta)
        return wd_meta


def normalize_workdir(workdir: KaosPath | str) -> str:
    """Resolve a work directory to the absolute form used as the metadata key."""
    try:
        return str(Path(str(workdir)).expanduser().resolve())
    except Exception:
        return str(workdir)


def _coerce_work_dirs(data: Any) -> list[dict[str, Any]]:
    """Normalise both known on-disk shapes into the canonical list form.

    Stores ``work_dirs`` as a list of :class:`WorkDirMeta` dicts. The
    removed ``coderai/cli/metadata.py`` stored it as ``{path: {...}}``. Accept
    either so an existing ``coderai.json`` is not silently discarded.
    """
    raw = data.get("work_dirs") if isinstance(data, dict) else None
    if isinstance(raw, list):
        return [wd for wd in raw if isinstance(wd, dict)]
    if isinstance(raw, dict):
        coerced: list[dict[str, Any]] = []
        for path, entry in raw.items():
            if isinstance(entry, dict):
                session_id = entry.get("last_session_id")
            else:
                session_id = entry
            coerced.append(
                {
                    "path": str(path),
                    "kaos": local_kaos.name,
                    "last_session_id": session_id or None,
                }
            )
        return coerced
    return []


def load_metadata() -> Metadata:
    metadata_file = get_metadata_file()
    if not metadata_file.exists():
        return Metadata()
    try:
        with open(metadata_file, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = {**data, "work_dirs": _coerce_work_dirs(data)}
        return Metadata(**data)
    except Exception:
        return Metadata()


def save_metadata(metadata: Metadata) -> None:
    metadata_file = get_metadata_file()
    try:
        atomic_json_write(metadata.model_dump(), metadata_file)
    except Exception as e:
        logger.warning("Failed to save metadata: %s", e)


def record_last_session(workdir: KaosPath | str, session_id: str) -> None:
    """Remember ``session_id`` as the latest session for ``workdir``.

    Update the
    existing work-directory record, creating it on first use.
    """
    if not session_id:
        return
    path = normalize_workdir(workdir)
    metadata = load_metadata()
    work_dir_meta = metadata.get_work_dir_meta(path)
    if work_dir_meta is None:
        work_dir_meta = metadata.new_work_dir_meta(path)
    work_dir_meta.last_session_id = session_id
    save_metadata(metadata)


def get_last_session_id(workdir: KaosPath | str) -> str | None:
    """Return the last session id recorded for ``workdir``, if any."""
    work_dir_meta = load_metadata().get_work_dir_meta(normalize_workdir(workdir))
    if work_dir_meta is None:
        return None
    return work_dir_meta.last_session_id or None
