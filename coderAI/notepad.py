"""Shared notepad for inter-agent communication."""

import threading
import time
from typing import Any, Dict, List, Optional


class SharedNotepad:
    """Thread-safe shared notepad for inter-agent communication.

    Allows multiple agents (parent + sub-agents) to share findings,
    intermediate results, and coordination notes.
    """

    def __init__(self):
        self._notes: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def write(self, key: str, value: str, author: str = "agent") -> None:
        """Write or overwrite a note."""
        with self._lock:
            self._notes[key] = {
                "value": value,
                "author": author,
                "timestamp": time.time(),
            }

    def read(self, key: str) -> Optional[Dict[str, Any]]:
        """Read a specific note by key."""
        with self._lock:
            return self._notes.get(key)

    def read_all(self) -> Dict[str, Dict[str, Any]]:
        """Read all notes."""
        with self._lock:
            return dict(self._notes)

    def list_keys(self) -> List[str]:
        """List all note keys."""
        with self._lock:
            return list(self._notes.keys())

    def delete(self, key: str) -> bool:
        """Delete a note. Returns True if it existed."""
        with self._lock:
            return self._notes.pop(key, None) is not None

    def clear(self) -> int:
        """Clear all notes. Returns count deleted."""
        with self._lock:
            count = len(self._notes)
            self._notes.clear()
            return count


# Global singleton shared across all agents in the process
shared_notepad = SharedNotepad()
