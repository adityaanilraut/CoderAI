"""Skill tool handler — loads full instructions for an available skill."""

from __future__ import annotations

import inspect
from typing import Any

from coderai.core.tools.types import ToolExecutionContext, ToolResult, as_str


async def handle_skill_tool(
    args: dict[str, Any], context: ToolExecutionContext | Any
) -> ToolResult:
    """Handle dynamic 'skill' tool call to load skill instructions into active context."""
    name = as_str(args.get("name")).strip()
    if not name:
        return ToolResult(
            ok=False,
            name="skill",
            error="ValidationError: Missing or empty 'name' parameter for skill tool.",
        )

    on_load_skill = getattr(context, "on_load_skill", None) or (
        context.get("on_load_skill") if isinstance(context, dict) else None
    )

    if not callable(on_load_skill):
        return ToolResult(
            ok=False,
            name="skill",
            error="Skill loading is not available in this context.",
        )

    try:
        res = on_load_skill(name)
        if inspect.iscoroutine(res):
            res = await res
        if isinstance(res, ToolResult):
            return res
        if isinstance(res, dict):
            return ToolResult(
                ok=res.get("ok", True),
                name="skill",
                output=res.get("output"),
                error=res.get("error"),
                metadata=res.get("metadata"),
            )
        return ToolResult(ok=True, name="skill", output=str(res))
    except Exception as e:
        return ToolResult(
            ok=False,
            name="skill",
            error=f"Failed to load skill '{name}': {e}",
        )
