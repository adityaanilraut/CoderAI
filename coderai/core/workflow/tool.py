"""Model-facing workflow tool handler."""

from __future__ import annotations

import uuid
from typing import Any

from coderai.core.tools.types import ToolExecutionContext, ToolResult, as_str
from coderai.core.workflow.engine import WorkflowContext, execute_workflow_script


async def handle_workflow_tool(args: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Execute a structured multi-agent orchestration script."""
    script = as_str(args.get("script", "")).strip()
    if not script:
        return ToolResult(
            ok=False,
            name="workflow",
            error="Missing required parameter 'script'.",
        )

    meta_obj = args.get("meta")
    meta: dict[str, Any] = meta_obj if isinstance(meta_obj, dict) else {}
    workflow_name = as_str(meta.get("name", "")).strip() or "Workflow"
    args_obj = args.get("args")
    workflow_args: dict[str, Any] = args_obj if isinstance(args_obj, dict) else {}

    workflow_id = f"wf_{uuid.uuid4().hex[:8]}"
    wf_context = WorkflowContext(
        workflow_id=workflow_id,
        name=workflow_name,
        project_root=context.project_root,
        create_openai_client=context.create_openai_client,
        parent_session_id=context.session_id,
    )

    # Pre-populate phases if provided in meta
    meta_phases = meta.get("phases")
    if isinstance(meta_phases, list) and meta_phases:
        wf_context.log(f"Configured planned phases: {', '.join(str(p) for p in meta_phases)}")

    result = await execute_workflow_script(script, workflow_args, wf_context)

    meta_data = {
        "runId": result.workflow_id,
        "agentsStarted": result.agent_executions,
        "result": result.output,
        **result.to_dict(),
    }

    if result.status == "completed":
        return ToolResult(
            ok=True,
            name="workflow",
            output=result.format_markdown(),
            metadata=meta_data,
        )
    else:
        return ToolResult(
            ok=False,
            name="workflow",
            error=result.error or f"Workflow execution {result.status}.",
            output=result.format_markdown(),
            metadata=meta_data,
        )
