"""Tool handlers for Agent Teams and Swarm Coordination."""

from __future__ import annotations

from typing import Any

from coderai.core.teams.manager import get_team_manager
from coderai.core.tools.types import ToolExecutionContext, ToolResult, as_str


async def handle_spawn_teammate_tool(
    args: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    """Spawn a dedicated role-based teammate in the multi-agent swarm."""
    del context
    name = as_str(args.get("name", "")).strip()
    role = as_str(args.get("role", "")).strip()

    if not name or not role:
        return ToolResult(
            ok=False,
            name="spawn_teammate",
            error="Arguments 'name' and 'role' are required to spawn a teammate.",
        )

    system_prompt = as_str(args.get("system_prompt") or args.get("prompt", "")).strip() or None
    mode = str(args.get("mode") or "general").strip().lower()
    allowed_tools = (
        args.get("allowed_tools") if isinstance(args.get("allowed_tools"), list) else None
    )

    mgr = get_team_manager()
    teammate = mgr.spawn_teammate(
        name=name,
        role=role,
        system_prompt=system_prompt,
        mode=mode,
        allowed_tools=allowed_tools,
    )

    out = teammate.to_dict()
    md = (
        f"### Spawned Teammate: {name} (`{teammate.teammate_id}`)\n"
        f"- **Role**: `{role}`\n"
        f"- **Mode**: `{mode}`\n"
        f"- **Status**: `{teammate.status}`\n"
        f"- Use `team_task_create` or `team_task_update` to assign work to `{name}`."
    )

    return ToolResult(
        ok=True,
        name="spawn_teammate",
        output=md,
        metadata=out,
    )


async def handle_team_task_create_tool(
    args: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    """Create a task on the shared team task board."""
    del context
    title = as_str(args.get("title", "")).strip()
    description = as_str(args.get("description", "")).strip()

    if not title:
        return ToolResult(
            ok=False,
            name="team_task_create",
            error="Argument 'title' is required.",
        )

    assigned_to = as_str(args.get("assigned_to", "")).strip() or None
    priority = as_str(args.get("priority", "medium")).strip().lower()
    dependencies = args.get("dependencies") if isinstance(args.get("dependencies"), list) else None

    mgr = get_team_manager()
    task = mgr.task_board.create_task(
        title=title,
        description=description,
        assigned_to=assigned_to,
        priority=priority,
        dependencies=dependencies,
    )

    out = task.to_dict()
    dep_str = ", ".join(task.dependencies) if task.dependencies else "None"
    md = (
        f"### Task Created: [{task.task_id}] {task.title}\n"
        f"- **Status**: `{task.status}`\n"
        f"- **Priority**: `{task.priority}`\n"
        f"- **Assigned To**: `{task.assigned_to or 'Unassigned'}`\n"
        f"- **Dependencies**: `{dep_str}`\n"
        f"- **Description**: {task.description or 'No description'}"
    )

    return ToolResult(
        ok=True,
        name="team_task_create",
        output=md,
        metadata=out,
    )


async def handle_team_task_get_tool(
    args: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    """Retrieve details of a task from the shared team task board."""
    del context
    task_id = as_str(args.get("task_id", "")).strip()
    if not task_id:
        return ToolResult(
            ok=False,
            name="team_task_get",
            error="Argument 'task_id' is required.",
        )

    mgr = get_team_manager()
    task = mgr.task_board.get_task(task_id)
    if not task:
        return ToolResult(
            ok=False,
            name="team_task_get",
            error=f"Task '{task_id}' not found.",
        )

    out = task.to_dict()
    dep_str = ", ".join(task.dependencies) if task.dependencies else "None"
    can_start = mgr.task_board.can_start_task(task_id)
    md = [
        f"### Task Details: [{task.task_id}] {task.title}",
        f"- **Status**: `{task.status}` (Dependencies Met: `{can_start}`)",
        f"- **Priority**: `{task.priority}`",
        f"- **Assigned To**: `{task.assigned_to or 'Unassigned'}`",
        f"- **Dependencies**: `{dep_str}`",
        f"- **Description**: {task.description or 'None'}",
    ]
    if task.result:
        md.append(f"\n**Result**:\n{task.result}")
    if task.notes:
        md.append(f"\n**Notes**:\n{task.notes}")

    return ToolResult(
        ok=True,
        name="team_task_get",
        output="\n".join(md),
        metadata=out,
    )


async def handle_team_task_list_tool(
    args: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    """List tasks on the shared team task board with optional filters."""
    del context
    status = as_str(args.get("status", "")).strip() or None
    assigned_to = as_str(args.get("assigned_to", "")).strip() or None

    mgr = get_team_manager()
    tasks = mgr.task_board.list_tasks(status=status, assigned_to=assigned_to)

    if not tasks:
        return ToolResult(
            ok=True,
            name="team_task_list",
            output="No tasks found matching query.",
            metadata={"tasks": []},
        )

    lines = [
        "### Team Task Board",
        "| ID | Title | Status | Priority | Assignee | Dependencies |",
        "|---|---|---|---|---|---|",
    ]
    for t in tasks:
        deps = ",".join(t.dependencies) if t.dependencies else "-"
        lines.append(
            f"| `{t.task_id}` | {t.title} | `{t.status}` | `{t.priority}` | `{t.assigned_to or '-'}` | {deps} |"
        )

    return ToolResult(
        ok=True,
        name="team_task_list",
        output="\n".join(lines),
        metadata={"tasks": [t.to_dict() for t in tasks]},
    )


async def handle_team_task_update_tool(
    args: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    """Update status, assignment, result, or notes of a task on the shared task board."""
    del context
    task_id = as_str(args.get("task_id", "")).strip()
    if not task_id:
        return ToolResult(
            ok=False,
            name="team_task_update",
            error="Argument 'task_id' is required.",
        )

    status = as_str(args.get("status", "")).strip() or None
    assigned_to = as_str(args.get("assigned_to", "")).strip() if "assigned_to" in args else None
    result = as_str(args.get("result", "")).strip() if "result" in args else None
    notes = as_str(args.get("notes", "")).strip() if "notes" in args else None

    mgr = get_team_manager()
    task = mgr.task_board.update_task(
        task_id=task_id,
        status=status,
        assigned_to=assigned_to,
        result=result,
        notes=notes,
    )

    if not task:
        return ToolResult(
            ok=False,
            name="team_task_update",
            error=f"Task '{task_id}' not found.",
        )

    out = task.to_dict()
    md = (
        f"### Updated Task [{task.task_id}] {task.title}\n"
        f"- **Status**: `{task.status}`\n"
        f"- **Assigned To**: `{task.assigned_to or 'Unassigned'}`\n"
        f"- **Result**: {task.result or 'None'}\n"
        f"- **Notes**: {task.notes or 'None'}"
    )

    return ToolResult(
        ok=True,
        name="team_task_update",
        output=md,
        metadata=out,
    )


async def handle_wait_agent_tool(args: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Wait for completion or message settlement from spawned teammates or subagents."""
    del context
    agent_id_raw = (
        args.get("agent_id") or args.get("agent_ids") or args.get("id") or args.get("teammate_id")
    )
    if not agent_id_raw:
        return ToolResult(
            ok=False,
            name="wait_agent",
            error="Missing required parameter 'agent_id' or 'agent_ids'.",
        )

    try:
        timeout_seconds = float(args.get("timeout_seconds", 60.0))
    except (ValueError, TypeError):
        timeout_seconds = 60.0

    wait_for = str(args.get("wait_for") or "completion").strip().lower()
    if wait_for not in ("completion", "message", "any_settlement"):
        wait_for = "completion"

    mgr = get_team_manager()
    res = await mgr.wait_agent(
        agent_ids=agent_id_raw,
        timeout_seconds=timeout_seconds,
        wait_for=wait_for,
    )

    agents_list = res.get("agents", [])
    lines = [
        f"### Wait Agent Result — {'✅ SETTLED' if res.get('ok') else '⏱️ TIMEOUT'}",
        f"**Wait Condition**: `{wait_for}` | **Elapsed**: `{res.get('elapsed_seconds', 0):.2f}s`\n",
    ]
    for a in agents_list:
        lines.append(
            f"- **Agent `{a.get('id')}`** [{a.get('status')}]: Settled=`{a.get('settled')}`"
        )

    return ToolResult(
        ok=res.get("ok", False),
        name="wait_agent",
        output="\n".join(lines),
        metadata=res,
        error=None if res.get("ok") else f"Wait timeout after {timeout_seconds}s.",
    )
