"""subagent / subagent_fork / send_message / interrupt_agent / list_agents / report."""

from __future__ import annotations

import asyncio
from typing import Any

from coderai.core.agents import (
    append_parent_session_notice,
    get_agent_registry,
    spawn_background_agent,
)
from coderai.core.orchestration import status_to_stop_reason
from coderai.core.subagent import MAX_SUBAGENT_DEPTH, SubAgentManager, SubAgentSpec
from coderai.core.tools.types import ToolExecutionContext, ToolResult, as_str

# job_id -> (manager, subagent_session_id, task) for background one-shot
# subagent jobs, so job_kill can stop the underlying worker.
_SUBAGENT_JOB_TASKS: dict[str, tuple[SubAgentManager, str, asyncio.Task[Any]]] = {}


def subagent_job_tasks() -> dict[str, tuple[SubAgentManager, str, asyncio.Task[Any]]]:
    return _SUBAGENT_JOB_TASKS


def _derive_depth(context: ToolExecutionContext, args: dict[str, Any]) -> int:
    """Lineage-derived delegation depth (parent + 1); monotone via the registry.

    The model-supplied ``depth`` argument remains a fallback for callers
    without a live registry handle (mirrors the harness: the persisted
    lineage is the authoritative floor, a child can never reset to zero).
    """
    for handle in get_agent_registry().list():
        if getattr(handle, "run_session_id", None) == context.session_id:
            return handle.depth + 1
    try:
        return int(args.get("depth", 0))
    except (ValueError, TypeError):
        return 0


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


def _start_subagent_job(
    *,
    context: ToolExecutionContext,
    manager: SubAgentManager,
    spec: SubAgentSpec,
    label: str,
    tool_name: str,
) -> ToolResult:
    """Start a background ONE-SHOT subagent as a ``subagent`` job.

    Mirrors the harness: the background child owns a job record collected
    with ``job_output`` and stopped with ``job_kill``; the completion notice
    is delivered to the parent session once.
    """
    from coderai.core.jobs import get_job_store

    store = get_job_store()
    session_id = context.session_id or ""
    with store._lock:
        counter = (
            sum(1 for j in store._jobs.values() if j.kind == "subagent")
            + 1
        )
        job_id = f"subagent-{counter}"
    store.start(
        job_id=job_id,
        session_id=session_id,
        kind="subagent",
        label=label[:240],
    )

    async def _run_job() -> None:
        try:
            result = await manager.spawn_subagent(spec)
        except asyncio.CancelledError:
            store.complete(job_id, ok=False, signal="SIGINT", detail="killed")
            return
        except Exception as exc:  # pragma: no cover - defensive
            store.complete(job_id, ok=False, detail=str(exc))
            return
        store.complete(
            job_id,
            ok=result.status == "completed",
            detail=result.status,
        )
        _SUBAGENT_JOB_TASKS.pop(job_id, None)
        notice = (
            f"Background job {job_id} finished [status: {result.status}, "
            f"{status_to_stop_reason(result.status)}]."
        )
        if result.summary:
            notice += f"\nResult: {result.summary}"
        from coderai.core.agents import notify_parent_session

        if not notify_parent_session(session_id, notice):
            append_parent_session_notice(
                context.project_root, session_id, notice, source="job-completion"
            )

    task = asyncio.create_task(_run_job())
    _SUBAGENT_JOB_TASKS[job_id] = (manager, spec_task_session(manager, spec), task)
    return ToolResult(
        ok=True,
        name=tool_name,
        output=f"started background subagent job {job_id}",
        metadata={"kind": "background", "jobId": job_id},
    )


def spec_task_session(manager: SubAgentManager, spec: SubAgentSpec) -> str:
    return (
        f"sub_{spec.parent_session_id[:8] if spec.parent_session_id else 'root'}_{spec.task_id}"
    )


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

    depth = _derive_depth(context, args)

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

    if args.get("run_in_background") is True:
        return _start_subagent_job(
            context=context,
            manager=manager,
            spec=spec,
            label=description,
            tool_name="subagent_fork",
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
    depth = _derive_depth(context, args)
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

    # run_in_background: false → one-shot foreground result (harness contract);
    # default true → durable continuable child id, resolved at inbox acceptance.
    if args.get("run_in_background") is False:
        result = await manager.spawn_subagent(spec)
        is_ok = result.status == "completed"
        return ToolResult(
            ok=is_ok,
            name="subagent",
            output=result.format_markdown(),
            error=result.error if not is_ok else None,
            metadata={"kind": "foreground", **result.to_dict()},
        )

    try:
        handle = await spawn_background_agent(manager, spec)
    except RuntimeError as exc:
        return ToolResult(ok=False, name="subagent", error=str(exc))
    return ToolResult(
        ok=True,
        name="subagent",
        output=(
            f"Started background sub-agent {handle.id} ({description}). "
            "Use send_message to continue it, list_agents to inspect, interrupt_agent to cancel."
        ),
        metadata={"kind": "continuable", "subagentId": handle.id, **handle.to_public_dict()},
    )


def _caller_owns_target(context: ToolExecutionContext, target: Any) -> bool:
    """Authorize a control call: the caller must be the target's direct parent
    session (follow-up authority) or a live ancestor session (interrupt
    authority), mirroring the harness lineage checks."""
    registry = get_agent_registry()
    caller_session = str(getattr(context, "session_id", "") or "")
    if not caller_session or not target:
        return False
    if getattr(target, "parent_session_id", "") == caller_session:
        return True
    # Walk the recorded parent chain: an ancestor agent whose run session is
    # the caller's session may also control the target.
    current = registry.get(getattr(target, "parent_agent_id", "") or "")
    seen: set[str] = set()
    while current is not None and current.id not in seen:
        seen.add(current.id)
        if getattr(current, "run_session_id", None) == caller_session:
            return True
        current = registry.get(current.parent_agent_id or "")
    return False


async def handle_send_message_tool(
    args: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    agent_id = as_str(
        args.get("subagent_id") or args.get("agent_id") or args.get("id")
    ).strip()
    message = as_str(args.get("message") or args.get("prompt")).strip()
    if not agent_id or not message:
        return ToolResult(ok=False, name="send_message", error="subagent_id and message are required.")
    handle = get_agent_registry().get(agent_id)
    if handle is None:
        return ToolResult(ok=False, name="send_message", error=f"Unknown agent '{agent_id}'.")
    if not _caller_owns_target(context, handle):
        return ToolResult(
            ok=False,
            name="send_message",
            error=f"UNAUTHORIZED: agent '{agent_id}' is not a child of this session.",
        )
    get_agent_registry().send(agent_id, message)
    return ToolResult(
        ok=True,
        name="send_message",
        output=f"Queued message for {agent_id} [{handle.status}]. Inbox size: {len(handle.inbox)}.",
        metadata=handle.to_public_dict(),
    )


async def handle_interrupt_agent_tool(
    args: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    agent_id = as_str(args.get("agent_id") or args.get("id")).strip()
    if not agent_id:
        return ToolResult(ok=False, name="interrupt_agent", error="agent_id is required.")
    handle = get_agent_registry().get(agent_id)
    if handle is None:
        return ToolResult(ok=False, name="interrupt_agent", error=f"Unknown agent '{agent_id}'.")
    if not _caller_owns_target(context, handle):
        return ToolResult(
            ok=False,
            name="interrupt_agent",
            error=f"UNAUTHORIZED: agent '{agent_id}' is not a live descendant of this session.",
        )
    get_agent_registry().interrupt(agent_id)
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
    lines = []
    for a in agents:
        line = f"{a.id} [{a.status}] {a.mode} — {a.description}"
        summary = a.report or (a.result.summary if a.result else None)
        if summary:
            line += f"\n  Result: {summary}"
        lines.append(line)
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
