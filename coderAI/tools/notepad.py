"""Notepad tool for inter-agent communication."""

import logging
import threading
import time
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from .base import Tool

logger = logging.getLogger(__name__)


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

    def list_keys(self) -> list:
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


class NotepadParams(BaseModel):
    action: str = Field(
        ...,
        description=(
            "Action: 'write' (store a note), 'read' (read a specific note by key), "
            "'list' (list all note keys), 'read_all' (read all notes), "
            "'delete' (remove a note), 'clear' (remove all notes)."
        ),
    )
    key: Optional[str] = Field(
        None,
        description="Note key/identifier (required for 'write', 'read', 'delete').",
    )
    value: Optional[str] = Field(
        None,
        description="Note content (required for 'write').",
    )


class NotepadTool(Tool):
    """Shared notepad for inter-agent communication."""

    name = "notepad"
    description = (
        "Read and write a shared scratchpad across the main agent and sub-agents. Use this "
        "when you want to stash findings, coordination notes, or intermediate results under "
        "stable keys. Do not use it as a step tracker; use plan for that. Example: "
        "action='write', key='auth_findings', value='refresh token flow lives in auth.py'."
    )
    parameters_model = NotepadParams
    is_read_only = False

    async def execute(
        self,
        action: str,
        key: Optional[str] = None,
        value: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            if action == "write":
                if not key:
                    return {"success": False, "error": "key is required for 'write'."}
                if value is None:
                    return {"success": False, "error": "value is required for 'write'."}
                shared_notepad.write(key, value)
                return {"success": True, "message": f"Note '{key}' saved."}

            elif action == "read":
                if not key:
                    return {"success": False, "error": "key is required for 'read'."}
                note = shared_notepad.read(key)
                if note is None:
                    return {"success": False, "error": f"Note '{key}' not found."}
                return {"success": True, "key": key, "note": note}

            elif action == "list":
                keys = shared_notepad.list_keys()
                return {"success": True, "keys": keys, "count": len(keys)}

            elif action == "read_all":
                notes = shared_notepad.read_all()
                return {"success": True, "notes": notes, "count": len(notes)}

            elif action == "delete":
                if not key:
                    return {"success": False, "error": "key is required for 'delete'."}
                existed = shared_notepad.delete(key)
                if existed:
                    return {"success": True, "message": f"Note '{key}' deleted."}
                return {"success": False, "error": f"Note '{key}' not found."}

            elif action == "clear":
                count = shared_notepad.clear()
                return {"success": True, "message": f"Cleared {count} note(s)."}

            else:
                return {
                    "success": False,
                    "error": f"Unknown action: {action}. Use 'write', 'read', 'list', 'read_all', 'delete', or 'clear'.",
                }

        except Exception as e:
            return {"success": False, "error": str(e)}
