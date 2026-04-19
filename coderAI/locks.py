"""Global resource locking mechanism for parallel agent orchestration."""

import asyncio
import weakref
from pathlib import Path
from typing import Optional


class ResourceManager:
    """Provides asyncio locks to prevent race conditions during parallel execution."""

    def __init__(self):
        self._file_locks = weakref.WeakValueDictionary()
        self._file_locks_lock: Optional[asyncio.Lock] = None
        self._git_lock: Optional[asyncio.Lock] = None
        self._workspace_lock: Optional[asyncio.Lock] = None

    def _ensure_locks(self) -> None:
        """Lazily create locks inside a running event loop (safe for Python 3.9+)."""
        if self._git_lock is None:
            self._file_locks_lock = asyncio.Lock()
            self._git_lock = asyncio.Lock()
            self._workspace_lock = asyncio.Lock()

    async def get_file_lock(self, filepath: str) -> asyncio.Lock:
        """Get or create an asyncio Lock for a specific absolute or relative filepath."""
        self._ensure_locks()
        try:
            # Normalize path completely to ensure no aliases bypass the lock
            normalized_path = str(Path(filepath).resolve())
        except Exception:
            normalized_path = str(filepath)

        async with self._file_locks_lock:
            lock = self._file_locks.get(normalized_path)
            if lock is None:
                lock = asyncio.Lock()
                self._file_locks[normalized_path] = lock
            return lock

    def git_lock(self) -> asyncio.Lock:
        """Lock to prevent concurrent git modifications that cause index.lock errors."""
        self._ensure_locks()
        return self._git_lock

    def workspace_lock(self) -> asyncio.Lock:
        """Lock for major operations that interact with the whole workspace (e.g. tests)."""
        self._ensure_locks()
        return self._workspace_lock


# Global singleton instance
resource_manager = ResourceManager()
