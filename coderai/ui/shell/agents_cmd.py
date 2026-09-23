"""Live subagent inspection for /agents tree, report, and send."""

from __future__ import annotations

from typing import Any

from coderai.subagents.core import AgentHandle, AgentRegistry, get_agent_registry


def registry_for(manager: Any) -> AgentRegistry:
    """Prefer the session manager's registry; fall back to the process singleton."""
    registry = getattr(manager, "agent_registry", None)
    if isinstance(registry, AgentRegistry):
        return registry
    return get_agent_registry()


def handles_for_session(registry: AgentRegistry, session_id: str | None) -> list[AgentHandle]:
    if session_id:
        scoped = registry.list(parent_session_id=session_id)
        if scoped:
            return scoped
    return registry.list()


def format_tree(handles: list[AgentHandle]) -> str:
    """Plain-text tree of the given handles."""
    if not handles:
        return "No live subagents in this session."
    by_id = {handle.id: handle for handle in handles}
    roots = [
        handle
        for handle in handles
        if not handle.parent_agent_id or handle.parent_agent_id not in by_id
    ]
    lines: list[str] = ["Subagent tree:"]

    def walk(handle: AgentHandle, indent: str) -> None:
        lines.append(f"{indent}{handle.id} [{handle.status}] {_clip(handle.description)}")
        for child_id in handle.children_ids:
            child = by_id.get(child_id)
            if child is not None:
                walk(child, indent + "  ")

    for root in roots:
        walk(root, "")
    return "\n".join(lines)


def format_report(handle: AgentHandle | None, agent_id: str) -> str:
    if handle is None:
        return f"Unknown agent {agent_id!r}. Use /agents tree to list live runs."
    lines = [
        f"{handle.id} [{handle.status}] depth={handle.depth}",
        f"description: {handle.description or '(none)'}",
    ]
    if handle.report:
        lines.append(handle.report)
    elif handle.result is not None:
        summary = getattr(handle.result, "summary", "") or ""
        error = getattr(handle.result, "error", None)
        lines.append(summary or "(no summary)")
        if error:
            lines.append(f"error: {error}")
    else:
        lines.append("(no report yet)")
    return "\n".join(lines)


def send_to_agent(registry: AgentRegistry, agent_id: str, message: str) -> str:
    handle = registry.send(agent_id, message)
    if handle is None:
        return f"Unknown agent {agent_id!r}. Use /agents tree to list live runs."
    return f"Sent to {handle.id} ({len(handle.inbox)} queued)."


def _clip(text: str, limit: int = 80) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact or "(no description)"
    return compact[: limit - 1] + "..."
