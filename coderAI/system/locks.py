"""Global resource locking mechanism for parallel agent orchestration."""

import asyncio
from collections import OrderedDict
from pathlib import Path
from typing import Optional

# Cap the file-lock table so long-running sessions don't leak entries for
# every file ever touched. When exceeded we evict the least-recently-used
# entries that are not currently held.
_MAX_FILE_LOCKS = 1024


class ResourceManager:
    """Provides asyncio locks to prevent race conditions during parallel execution.

    Uses a bounded LRU-style dict rather than a ``WeakValueDictionary``:
    with weak references a lock could be garbage-collected between a
    ``get_file_lock`` return and the caller's ``async with``, causing two
    concurrent writers to end up with *different* lock objects.
    """

    def __init__(self):
        self._file_locks: "OrderedDict[str, asyncio.Lock]" = OrderedDict()
        self._file_locks_lock: Optional[asyncio.Lock] = None
        self._git_lock: Optional[asyncio.Lock] = None
        self._loop_id: Optional[int] = None

    def _ensure_locks(self) -> None:
        """Lazily create or recreate locks inside the running event loop."""
        loop = asyncio.get_running_loop()
        loop_id = id(loop)
        if self._git_lock is None or self._loop_id != loop_id:
            self._file_locks_lock = asyncio.Lock()
            self._git_lock = asyncio.Lock()
            self._file_locks.clear()
            self._loop_id = loop_id

    async def get_file_lock(self, filepath: str) -> asyncio.Lock:
        """Get or create an asyncio Lock for a specific absolute or relative filepath."""
        self._ensure_locks()
        try:
            normalized_path = str(Path(filepath).resolve())
        except (OSError, RuntimeError):
            normalized_path = str(filepath)

        assert self._file_locks_lock is not None
        async with self._file_locks_lock:
            lock = self._file_locks.get(normalized_path)
            if lock is None:
                lock = asyncio.Lock()
                self._file_locks[normalized_path] = lock
            else:
                # Mark as recently used for LRU eviction
                self._file_locks.move_to_end(normalized_path)

            # Best-effort eviction of idle (unlocked) entries
            if len(self._file_locks) > _MAX_FILE_LOCKS:
                self._evict_idle_locks()
            return lock

    def _evict_idle_locks(self) -> None:
        """Evict unlocked entries from the front of the LRU."""
        to_remove: list[str] = []
        for path, lock in self._file_locks.items():
            if len(self._file_locks) - len(to_remove) <= _MAX_FILE_LOCKS:
                break
            if not lock.locked():
                to_remove.append(path)
        for path in to_remove:
            self._file_locks.pop(path, None)

    def git_lock(self) -> asyncio.Lock:
        """Lock to prevent concurrent git modifications that cause index.lock errors."""
        self._ensure_locks()
        assert self._git_lock is not None
        return self._git_lock


# Global singleton instance
resource_manager = ResourceManager()
