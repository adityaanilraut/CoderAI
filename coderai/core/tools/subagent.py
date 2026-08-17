"""Sub-Agent Task tool handler for CoderAI."""

from __future__ import annotations

from typing import Any

from coderai.core.subagent import SubAgentManager, SubAgentSpec
from coderai.core.tools.types import ToolExecutionContext, ToolResult, as_str


async def handle_subagent_tool(args: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Execute a sub-agent task in an isolated child session."""
    description = as_str(args.get("description", "")).strip()
    prompt = as_str(args.get("prompt", "")).strip()

    if not description:
        return ToolResult(
            ok=False,
            name="Task",
            error="Missing required argument 'description'. Provide a 3-5 word summary of the sub-task.",
        )

    if not prompt:
        return ToolResult(
            ok=False,
            name="Task",
            error="Missing required argument 'prompt'. Provide detailed instructions and context for the sub-agent.",
        )

    mode = str(args.get("mode") or "read_only").strip().lower()
    if mode not in ("read_only", "general"):
        mode = "read_only"

    timeout_raw = args.get("timeout_seconds")
    try:
        timeout_seconds = float(timeout_raw) if timeout_raw is not None else 90.0
    except (ValueError, TypeError):
        timeout_seconds = 90.0

    if not context.create_openai_client:
        return ToolResult(
            ok=False,
            name="Task",
            error="SubAgentExecutionError: Client factory not available in execution context.",
        )

    manager = SubAgentManager(
        project_root=context.project_root,
        create_openai_client=context.create_openai_client,
    )

    spec = SubAgentSpec(
        description=description,
        prompt=prompt,
        mode=mode,
        timeout_seconds=timeout_seconds,
        parent_session_id=context.session_id,
        extra_context=as_str(args.get("context", "")).strip() or None,
    )

    result = await manager.spawn_subagent(spec)

    is_ok = result.status == "completed"
    return ToolResult(
        ok=is_ok,
        name="Task",
        output=result.format_markdown(),
        error=result.error if not is_ok else None,
        metadata=result.to_dict(),
    )


# Alias for handler discovery
handle = handle_subagent_tool
