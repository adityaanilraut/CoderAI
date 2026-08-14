"""Tool for analyzing active context window token utilization and layer breakdown."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from coderAI.core.services import get_services
from coderAI.llm.base import estimate_tokens_by_chars
from coderAI.tools.base import Tool
from coderAI.types.tool_error_codes import ToolErrorCode


class ContextStatsParams(BaseModel):
    include_layers: bool = Field(
        True, description="Whether to include granular per-layer token breakdown"
    )
    include_pinned_files: bool = Field(
        True, description="Whether to list individual pinned context files"
    )


class _CharEstimateProvider:
    """Fallback token counter when no live agent controller is bound."""

    MODEL_CONTEXT_WINDOWS: dict[str, int] = {}

    def count_tokens(self, text: str) -> int:
        return estimate_tokens_by_chars(text or "")


class ContextStatsTool(Tool):
    """Inspect active context token usage, layer distribution, and memory pressure."""

    name = "context_stats"
    description = (
        "Analyze context window token consumption, layer distribution (system prompt, pinned files, "
        "conversation history, tool outputs), and receive proactive compaction recommendations."
    )
    parameters_model = ContextStatsParams
    is_read_only = True
    requires_confirmation = False
    category = "utilities"

    async def execute(  # type: ignore[override]
        self,
        include_layers: bool = True,
        include_pinned_files: bool = True,
    ) -> dict[str, Any]:
        try:
            services = get_services()
            history_mgr = services.history
            session = history_mgr.current_session
            messages = [m.model_dump() for m in session.messages] if session else []

            controller = services.context_controller
            live = controller is not None
            if controller is None:
                from coderAI.context.context_controller import ContextController

                controller = ContextController(
                    provider=_CharEstimateProvider(),  # type: ignore[arg-type]
                    config=services.config,
                )

            breakdown = controller.get_context_breakdown(messages)

            result: dict[str, Any] = {
                "success": True,
                "live_controller": live,
                "total_tokens": breakdown["total_tokens"],
                "context_limit": breakdown["context_limit"],
                "utilization_pct": breakdown["utilization_pct"],
                "recommendation": breakdown["recommendation"],
                "message_count": breakdown["message_count"],
            }

            if include_layers:
                result["layers"] = breakdown["layers"]

            if include_pinned_files:
                result["pinned_files"] = [
                    {"path": p, "size_chars": len(c)} for p, c in controller.pinned_files.items()
                ]

            return result
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }
