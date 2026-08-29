"""subagent / subagent_fork / send_message / interrupt_agent / list_agents / report."""

from __future__ import annotations

from typing import Any

from coderai.core.agents import get_agent_registry, spawn_background_agent
from coderai.core.subagent import MAX_SUBAGENT_DEPTH, SubAgentManager, SubAgentSpec
from coderai.core.tools.types import ToolExecutionContext, ToolResult, as_str


def _extract_seed_messages(context: ToolExecutionContext) -> list[dict[str, Any]]:
    """Extract completed conversation history from parent session to seed a forked sub-agent."""
    seed_messages: list[dict[str, Any]] = []
    if context.list_session_messages and context.session_id:
        try:
            parent_msgs = context.list_session_messages(context.session_id)
            for m in parent_msgs:
                role = getattr(m, "role", "") if hasattr(m, "role") else m.get("role", "")
                content = (
                    getattr(m, "content", "") if hasattr(m, "content") else m.get("content", "")
                )
                if role and role != "system" and content:
                    seed_messages.append({"role": str(role), "content": str(content)})
        except Exception:
            pass
    return seed_messages


async def handle_subagent_fork_tool(
    args: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    """Fork sub-agent seeded with parent's completed conversation context."""
    description = as_str(args.get("description", "")).strip() or "Forked sub-agent task"
    prompt = as_str(args.get("prompt", "")).strip()
    if not prompt:
        return ToolResult(
            ok=False,
            name="subagent_fork",
            error="Missing required argument 'prompt'. Provide detailed instructions for the forked sub-agent.",
        )
    if not context.create_openai_client:
        return ToolResult(
            ok=False,
            name="subagent_fork",
            error="SubAgentExecutionError: Client factory not available in execution context.",
        )

    mode = str(args.get("mode") or "read_only").strip().lower()
    if mode not in ("read_only", "general"):
        mode = "read_only"

    try:
        depth = int(args.get("depth", 0))
    except (ValueError, TypeError):
        depth = 0

    try:
        max_depth = int(args.get("max_depth")) if args.get("max_depth") is not None else MAX_SUBAGENT_DEPTH
    except (ValueError, TypeError):
        max_depth = MAX_SUBAGENT_DEPTH

    try:
        token_budget = int(args.get("token_budget")) if args.get("token_budget") is not None else None
    except (ValueError, TypeError):
        token_budget = None

    if depth >= max_depth:
        return ToolResult(
            ok=False,
            name="subagent_fork",
            error=f"RecursionLimitError: sub-agent depth cannot exceed {max_depth}.",
        )

    timeout_raw = args.get("timeout_seconds")
    try:
        timeout_seconds = float(timeout_raw) if timeout_raw is not None else 90.0
    except (ValueError, TypeError):
        timeout_seconds = 90.0

    seed_messages = _extract_seed_messages(context)

    manager = SubAgentManager(
        project_root=context.project_root,
        create_openai_client=context.create_openai_client,
    )
    spec = SubAgentSpec(
        description=description,
        prompt=prompt,
        mode=mode,
        depth=depth,
        max_depth=max_depth,
        token_budget=token_budget,
        timeout_seconds=timeout_seconds,
        parent_session_id=context.session_id,
        extra_context=as_str(args.get("context", "")).strip() or None,
        seed_messages=seed_messages if seed_messages else None,
    )

    result = await manager.spawn_subagent(spec)
    is_ok = result.status == "completed"
    return ToolResult(
        ok=is_ok,
        name="subagent_fork",
        output=result.format_markdown(),
        error=result.error if not is_ok else None,
        metadata={**result.to_dict(), "seededMessagesCount": len(seed_messages)},
    )


async def handle_continuable_subagent_tool(
    args: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    """Background-default continuable sub-agent. Returns an agent id immediately."""
    description = as_str(args.get("description", "")).strip()
    prompt = as_str(args.get("prompt", "")).strip()
    if not description or not prompt:
        return ToolResult(
            ok=False,
            name="subagent",
            error="Missing required arguments 'description' and 'prompt'.",
        )
    if not context.create_openai_client:
        return ToolResult(ok=False, name="subagent", error="Client factory not available.")
    mode = str(args.get("mode") or "read_only").strip().lower()
    if mode not in ("read_only", "general"):
        mode = "read_only"
    try:
        depth = int(args.get("depth", 0))
    except (ValueError, TypeError):
        depth = 0
    try:
        max_depth = int(args.get("max_depth")) if args.get("max_depth") is not None else MAX_SUBAGENT_DEPTH
    except (ValueError, TypeError):
        max_depth = MAX_SUBAGENT_DEPTH
    try:
        token_budget = int(args.get("token_budget")) if args.get("token_budget") is not None else None
    except (ValueError, TypeError):
        token_budget = None

    if depth >= max_depth:
        return ToolResult(
            ok=False,
            name="subagent",
            error=f"RecursionLimitError: sub-agent depth cannot exceed {max_depth}.",
        )
    timeout_raw = args.get("timeout_seconds")
    try:
        timeout_seconds = float(timeout_raw) if timeout_raw is not None else 90.0
    except (ValueError, TypeError):
        timeout_seconds = 90.0
    manager = SubAgentManager(
        project_root=context.project_root,
        create_openai_client=context.create_openai_client,
    )
    spec = SubAgentSpec(
        description=description,
        prompt=prompt,
        mode=mode,
        depth=depth,
        max_depth=max_depth,
        token_budget=token_budget,
        timeout_seconds=timeout_seconds,
        parent_session_id=context.session_id,
        extra_context=as_str(args.get("context", "")).strip() or None,
    )
    handle = await spawn_background_agent(manager, spec)
    return ToolResult(
        ok=True,
        name="subagent",
        output=(
            f"Started background sub-agent {handle.id} ({description}). "
            "Use send_message to continue it, list_agents to inspect, interrupt_agent to cancel."
        ),
        metadata=handle.to_public_dict(),
    )


async def handle_send_message_tool(
    args: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    del context
    agent_id = as_str(args.get("agent_id") or args.get("id")).strip()
    message = as_str(args.get("message") or args.get("prompt")).strip()
    if not agent_id or not message:
        return ToolResult(ok=False, name="send_message", error="agent_id and message are required.")
    handle = get_agent_registry().send(agent_id, message)
    if handle is None:
        return ToolResult(ok=False, name="send_message", error=f"Unknown agent '{agent_id}'.")
    return ToolResult(
        ok=True,
        name="send_message",
        output=f"Queued message for {agent_id} [{handle.status}]. Inbox size: {len(handle.inbox)}.",
        metadata=handle.to_public_dict(),
    )


async def handle_interrupt_agent_tool(
    args: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    del context
    agent_id = as_str(args.get("agent_id") or args.get("id")).strip()
    if not agent_id:
        return ToolResult(ok=False, name="interrupt_agent", error="agent_id is required.")
    handle = get_agent_registry().interrupt(agent_id)
    if handle is None:
        return ToolResult(ok=False, name="interrupt_agent", error=f"Unknown agent '{agent_id}'.")
    return ToolResult(
        ok=True,
        name="interrupt_agent",
        output=f"Interrupted agent {agent_id}.",
        metadata=handle.to_public_dict(),
    )


async def handle_list_agents_tool(
    args: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    del args
    agents = get_agent_registry().list(context.session_id)
    if not agents:
        return ToolResult(ok=True, name="list_agents", output="(no sub-agents)")
    lines = [f"{a.id} [{a.status}] {a.mode} — {a.description}" for a in agents]
    return ToolResult(
        ok=True,
        name="list_agents",
        output="\n".join(lines),
        metadata={"agents": [a.to_public_dict() for a in agents]},
    )


async def handle_report_tool(args: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Child-only: record the sub-agent's final report for the parent."""
    summary = as_str(args.get("summary") or args.get("result")).strip()
    delivery = as_str(args.get("delivery") or "next-step").strip().lower()
    if delivery not in ("next-step", "quiet"):
        delivery = "next-step"
    if not summary:
        return ToolResult(ok=False, name="report", error="summary is required.")
    session_id = context.session_id
    for handle in get_agent_registry().list():
        if handle.spec and f"_{handle.spec.task_id}" in session_id:
            handle.report = summary
            break
    return ToolResult(
        ok=True,
        name="report",
        output=f"Report recorded for the parent agent (delivery: {delivery}).",
        metadata={"summary": summary, "delivery": delivery},
    )
