"""Notepad tool for inter-agent communication."""

import logging
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from .base import Tool
from ..notepad import shared_notepad

logger = logging.getLogger(__name__)


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
        "Read and write to a shared notepad that persists across tool calls and is "
        "shared between the main agent and all sub-agents. Useful for sharing findings, "
        "intermediate results, or coordination notes between agents. "
        "Actions: 'write' (key + value), 'read' (key), 'list', 'read_all', 'delete' (key), 'clear'."
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
                return {"success": True, "key": key, **note}

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
