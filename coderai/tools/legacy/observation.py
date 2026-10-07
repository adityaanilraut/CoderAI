"""Session-scoped raw file observations and fail-closed mutation verification."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def file_identity(path: str) -> tuple[int, int, int, int]:
    st = Path(path).stat()
    if not stat.S_ISREG(st.st_mode):
        raise OSError("Refusing to observe a non-regular file")
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns


def file_digest(path: str) -> str:
    digest = hashlib.sha256()
    from coderai.utils.path import open_regular_binary

    with open_regular_binary(path) as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class FileObservation:
    digest: str
    identity: tuple[int, int, int, int]
    encoding: str = "utf8"
    line_endings: Any = "LF"
    complete: bool = False


class FileObservationTracker:
    def __init__(self) -> None:
        self._observed: dict[str, dict[str, FileObservation]] = {}

    def _canonical(self, path: str) -> str:
        return str(Path(path).resolve())

    def get(self, session_id: str, path: str) -> FileObservation | None:
        return self._observed.get(session_id, {}).get(self._canonical(path))

    def record_observation(
        self,
        session_id: str | None,
        file_path: str,
        content: str | bytes | None = None,
        *,
        digest: str | None = None,
        identity: tuple[int, int, int, int] | None = None,
        encoding: str = "utf8",
        line_endings: Any = "LF",
        complete: bool = False,
    ) -> None:
        del content  # Normalized/display text is never a digest of on-disk bytes.
        sid = session_id or "default"
        canon = self._canonical(file_path)
        try:
            before = file_identity(canon)
            raw_digest = digest or file_digest(canon)
            after = file_identity(canon)
            if before != after or (identity is not None and identity != after):
                self._observed.get(sid, {}).pop(canon, None)
                return
            self._observed.setdefault(sid, {})[canon] = FileObservation(
                raw_digest, after, encoding, line_endings, complete
            )
        except OSError:
            self._observed.get(sid, {}).pop(canon, None)

    def check_mutation_allowed(
        self, session_id: str | None, file_path: str, require_observed: bool = True
    ) -> tuple[bool, str | None]:
        sid = session_id or "default"
        canon = self._canonical(file_path)
        if not os.path.exists(canon):
            return True, None
        observed = self.get(sid, canon)
        if observed is None:
            return (
                (False, f"FS_NOT_OBSERVED: File '{file_path}' must be read before modifying it.")
                if require_observed
                else (True, None)
            )
        try:
            before = file_identity(canon)
            current_digest = file_digest(canon)
            after = file_identity(canon)
            if (
                before != after
                or before[:2] != observed.identity[:2]
                or current_digest != observed.digest
            ):
                return (
                    False,
                    f"FS_STALE_VERSION: File '{file_path}' has changed since it was read. Read it again.",
                )
            return True, None
        except OSError as exc:
            return False, f"FS_VERIFICATION_FAILED: Unable to verify '{file_path}': {exc}"

    def clear_session(self, session_id: str) -> None:
        self._observed.pop(session_id, None)


_tracker = FileObservationTracker()


def get_observation_tracker() -> FileObservationTracker:
    return _tracker
