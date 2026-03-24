"""Global resource locking mechanism for parallel agent orchestration."""

import asyncio
from typing import Dict
from pathlib import Path

class ResourceManager:
    """Provides asyncio locks to prevent race conditions during parallel execution."""
    
    def __init__(self):
        self._file_locks: Dict[str, asyncio.Lock] = {}
        self._file_locks_lock = asyncio.Lock()  # Protects the _file_locks dict itself
        self._git_lock = asyncio.Lock()
        self._workspace_lock = asyncio.Lock()

    async def get_file_lock(self, filepath: str) -> asyncio.Lock:
        """Get or create an asyncio Lock for a specific absolute or relative filepath."""
        try:
            # Normalize path completely to ensure no aliases bypass the lock
            normalized_path = str(Path(filepath).resolve())
        except Exception:
            normalized_path = str(filepath)
            
        async with self._file_locks_lock:
            if normalized_path not in self._file_locks:
                self._file_locks[normalized_path] = asyncio.Lock()
            return self._file_locks[normalized_path]

    def git_lock(self) -> asyncio.Lock:
        """Lock to prevent concurrent git modifications that cause index.lock errors."""
        return self._git_lock

    def workspace_lock(self) -> asyncio.Lock:
        """Lock for major operations that interact with the whole workspace (e.g. tests)."""
        return self._workspace_lock


# Global singleton instance
resource_manager = ResourceManager()
