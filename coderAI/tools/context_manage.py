"""Tool for managing context."""

import logging
from typing import Any, Dict, Optional, Literal

from pydantic import BaseModel, Field

from coderAI.tools.base import Tool
from coderAI.context.context_controller import ContextController

logger = logging.getLogger(__name__)


class ManageContextParams(BaseModel):
    action: Literal["add", "remove", "list", "clear"] = Field(
        ..., description="Action to perform on context"
    )
    path: Optional[str] = Field(
        None, description="File path to add or remove (required for add/remove)"
    )


class ManageContextTool(Tool):
    """Tool for managing the agent's context (pinned files)."""

    name = "manage_context"
    description = (
        "Manage the agent's context by pinning important files or "
        "checking what is currently pinned."
    )
    category = "context"
    parameters_model = ManageContextParams
    safe = True

    def __init__(self, context_controller: ContextController):
        super().__init__()
        self.context_controller = context_controller

    async def execute(self, action: str, path: Optional[str] = None) -> Dict[str, Any]:  # type: ignore[override]
        """Execute context management action."""
        if action == "add":
            if not path:
                return {"success": False, "error": "Path required for add action"}

            success = self.context_controller.add_file(path)
            if success:
                return {"success": True, "message": f"Added {path} to pinned context."}
            else:
                return {
                    "success": False,
                    "error": f"Failed to add {path}. File may not exist or be too large.",
                }

        elif action == "remove":
            if not path:
                return {"success": False, "error": "Path required for remove action"}

            success = self.context_controller.remove_file(path)
            if success:
                return {"success": True, "message": f"Removed {path} from context."}
            else:
                return {"success": False, "error": f"{path} not found in context."}

        elif action == "list":
            files = self.context_controller.pinned_files
            instructions = self.context_controller.project_instructions
            token_est = self.context_controller.get_token_usage_estimate()

            return {
                "success": True,
                "pinned_files": list(files.keys()),
                "has_instructions": bool(instructions),
                "estimated_tokens": token_est,
            }

        elif action == "clear":
            self.context_controller.clear()
            return {"success": True, "message": "Cleared all pinned files."}

        return {"success": False, "error": f"Unknown action: {action}"}
