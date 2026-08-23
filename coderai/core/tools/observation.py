"""Filesystem Observation Policy — port of DeepSeek Harness fs-observation-policy.

Tracks authoritative read observations per session. Ensures an agent must observe
(read/view) an existing file before modifying it, and prevents mutations on stale
versions that have changed on disk since last observed.
"""

from __future__ import annotations

import hashlib
import os
import pathlib


class FileObservationTracker:
    """Session-scoped file observation state and anti-clobber guard."""

    def __init__(self) -> None:
        # Key: (session_id or "default", canonical_path) -> (mtime, sha256_hash)
        self._observed: dict[str, dict[str, tuple[float, str]]] = {}

    def _canonical(self, path: str) -> str:
        try:
            return str(pathlib.Path(path).resolve())
        except Exception:
            return os.path.abspath(path)

    def record_observation(
        self,
        session_id: str | None,
        file_path: str,
        content: str | bytes | None = None,
    ) -> None:
        """Record an authoritative presence observation for a file."""
        sid = session_id or "default"
        canon = self._canonical(file_path)
        if sid not in self._observed:
            self._observed[sid] = {}

        try:
            if not os.path.exists(canon):
                return
            mtime = os.path.getmtime(canon)
            if content is not None:
                raw = content.encode("utf-8") if isinstance(content, str) else content
                file_hash = hashlib.sha256(raw).hexdigest()
            else:
                with open(canon, "rb") as f:
                    file_hash = hashlib.sha256(f.read()).hexdigest()
            self._observed[sid][canon] = (mtime, file_hash)
        except Exception:
            pass

    def check_mutation_allowed(
        self,
        session_id: str | None,
        file_path: str,
        require_observed: bool = True,
    ) -> tuple[bool, str | None]:
        """Verify whether the calling session is permitted to write or edit the target file.

        Returns (is_allowed, error_message).
        """
        sid = session_id or "default"
        canon = self._canonical(file_path)

        # If file does not exist, creating a new file is permitted without prior read
        if not os.path.exists(canon):
            return True, None

        session_obs = self._observed.get(sid, {})
        if canon not in session_obs:
            if require_observed:
                return False, (
                    f"FS_NOT_OBSERVED: File '{file_path}' has not been read in this session. "
                    "You must view/read the file first to inspect its contents before modifying or overwriting it."
                )
            return True, None

        recorded_mtime, recorded_hash = session_obs[canon]
        try:
            current_mtime = os.path.getmtime(canon)
            # If mtime is identical, fast pass
            if current_mtime <= recorded_mtime:
                return True, None

            # If mtime differs, check hash to see if content actually changed
            with open(canon, "rb") as f:
                current_hash = hashlib.sha256(f.read()).hexdigest()
            if current_hash != recorded_hash:
                return False, (
                    f"FS_STALE_VERSION: File '{file_path}' has changed on disk since it was last read. "
                    "Re-read the file to observe its latest state, then retry your modification."
                )
            # Content is identical despite timestamp touch, update mtime
            self._observed[sid][canon] = (current_mtime, current_hash)
            return True, None
        except Exception:
            return True, None

    def clear_session(self, session_id: str) -> None:
        """Clear observation history for a closed session."""
        self._observed.pop(session_id, None)


_tracker = FileObservationTracker()


def get_observation_tracker() -> FileObservationTracker:
    """Return singleton observation tracker."""
    return _tracker
