"""exit_plan_mode — leave Plan Mode while mutation tools stay in the schema (KV-cache stability)."""

from __future__ import annotations

from typing import Any

from coderai.core.tools.types import ToolResult, as_str


def handle_exit_plan_mode_tool(args: dict[str, Any], context: Any) -> ToolResult:
    del context
    summary = as_str(args.get("summary") or args.get("plan") or "Exiting Plan Mode.").strip()
    return ToolResult(
        ok=True,
        name="exit_plan_mode",
        output=summary or "Exiting Plan Mode.",
        metadata={"exitPlanMode": True, "summary": summary},
    )
