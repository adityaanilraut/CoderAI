"""Per-session file-read deduplication cache.

Keeps a tiny ``{abs_path: (mtime, size, turn)}`` map so repeated reads of an
unchanged file inside one session can be short-circuited with a placeholder
instead of re-feeding the full content into chat history every turn.

The cache is per-``Agent`` (per session). It is cleared on
``Agent._reset_session_accounting`` so a new session starts fresh.

Partial reads (``start_line``/``end_line``) bypass the cache entirely — there
is no safe way to tell whether a future full read will see the same bytes.
"""

from typing import Dict, Optional, Tuple


class FileReadCache:
    """Tracks (path, mtime, size) → turn for full-file reads in this session."""

    def __init__(self) -> None:
        self._entries: Dict[str, Tuple[float, int, int]] = {}
        self._turn: int = 0

    @property
    def turn(self) -> int:
        return self._turn

    def bump_turn(self) -> int:
        """Advance the turn counter. Call once per user prompt."""
        self._turn += 1
        return self._turn

    def check(self, path: str, mtime: float, size: int) -> Optional[int]:
        """Return the turn at which an identical (path, mtime, size) was read.

        A return of ``None`` means cache miss — caller should perform a fresh
        read and call :meth:`record`.
        """
        entry = self._entries.get(path)
        if entry is None:
            return None
        prev_mtime, prev_size, prev_turn = entry
        if prev_mtime == mtime and prev_size == size:
            return prev_turn
        return None

    def record(self, path: str, mtime: float, size: int) -> None:
        """Record a fresh full-file read at the current turn."""
        # Ensure a turn boundary exists even if bump_turn was never called
        # (e.g. unit tests that exercise the tool directly).
        turn = self._turn if self._turn > 0 else 1
        self._entries[path] = (mtime, size, turn)

    def clear(self) -> None:
        """Drop all entries and reset the turn counter."""
        self._entries.clear()
        self._turn = 0
