"""Tool handler for code_mode."""

from __future__ import annotations

from typing import Any

from coderai.core.code_mode.engine import get_code_mode_sandbox
from coderai.core.tools.types import ToolExecutionContext, ToolResult, as_str


async def handle_code_mode_tool(args: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Execute Python code in the stateful session sandbox."""
    code = as_str(args.get("code", "")).strip()
    if not code:
        return ToolResult(
            ok=False,
            name="code_mode",
            error="Missing required argument 'code'.",
        )

    reset_state = bool(args.get("reset_state", False))
    try:
        timeout_seconds = float(args.get("timeout_seconds", 30.0))
        if timeout_seconds <= 0:
            timeout_seconds = 30.0
    except (ValueError, TypeError):
        timeout_seconds = 30.0

    sandbox = get_code_mode_sandbox(
        context.session_id,
        context.project_root,
        context.sandbox_mode,
    )
    if reset_state:
        sandbox.reset()

    result = await sandbox.execute(code, timeout_seconds=timeout_seconds)

    is_ok = result.error is None
    return ToolResult(
        ok=is_ok,
        name="code_mode",
        output=result.format_markdown(),
        metadata=result.to_dict(),
        error=result.error,
    )
