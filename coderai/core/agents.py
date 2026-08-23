"""Continuable sub-agent control plane (list / send / interrupt) over SubAgentManager."""

from __future__ import annotations

import asyncio
import builtins
import uuid
from dataclasses import dataclass, field
from typing import Any

from coderai.core.subagent import SubAgentManager, SubAgentResult, SubAgentSpec


@dataclass
class AgentHandle:
    id: str
    parent_session_id: str
    description: str
    mode: str
    status: str = "running"  # running | completed | interrupted | failed | timeout
    inbox: list[str] = field(default_factory=list)
    result: SubAgentResult | None = None
    task: asyncio.Task[Any] | None = None
    spec: SubAgentSpec | None = None
    report: str | None = None
    parent_agent_id: str | None = None
    root_agent_id: str | None = None
    depth: int = 0
    children_ids: list[str] = field(default_factory=list)
    lifecycle_history: list[dict[str, Any]] = field(default_factory=list)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "mode": self.mode,
            "status": self.status,
            "inbox": len(self.inbox),
            "parent_agent_id": self.parent_agent_id,
            "root_agent_id": self.root_agent_id,
            "depth": self.depth,
            "children_ids": list(self.children_ids),
        }


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, AgentHandle] = {}

    def register(self, handle: AgentHandle) -> AgentHandle:
        self._agents[handle.id] = handle
        return handle

    def get(self, agent_id: str) -> AgentHandle | None:
        return self._agents.get(agent_id)

    def list(self, parent_session_id: str | None = None) -> builtins.list[AgentHandle]:
        items = list(self._agents.values())
        if parent_session_id:
            items = [a for a in items if a.parent_session_id == parent_session_id]
        return items

    def get_children(self, agent_id: str) -> builtins.list[AgentHandle]:
        """Return direct child agents of the given agent."""
        parent = self._agents.get(agent_id)
        if not parent:
            return []
        return [self._agents[cid] for cid in parent.children_ids if cid in self._agents]

    def get_tree(self, agent_id: str) -> dict[str, Any] | None:
        """Return recursive tree dict of agent and all its descendants."""
        agent = self._agents.get(agent_id)
        if not agent:
            return None

        def _build_node(handle: AgentHandle) -> dict[str, Any]:
            children_nodes = []
            for cid in handle.children_ids:
                child = self._agents.get(cid)
                if child:
                    children_nodes.append(_build_node(child))
            return {
                "id": handle.id,
                "description": handle.description,
                "mode": handle.mode,
                "status": handle.status,
                "depth": handle.depth,
                "parent_agent_id": handle.parent_agent_id,
                "root_agent_id": handle.root_agent_id,
                "children": children_nodes,
            }

        return _build_node(agent)

    def send(self, agent_id: str, message: str) -> AgentHandle | None:
        handle = self._agents.get(agent_id)
        if handle is None:
            return None
        handle.inbox.append(message)
        return handle

    def interrupt(self, agent_id: str) -> AgentHandle | None:
        handle = self._agents.get(agent_id)
        if handle is None:
            return None
        handle.status = "interrupted"
        if handle.task and not handle.task.done():
            handle.task.cancel()
        return handle

    def interrupt_tree(self, agent_id: str) -> builtins.list[str]:
        """Recursively cancel an agent and all its child subagents."""
        interrupted: builtins.list[str] = []

        def _cancel_rec(aid: str) -> None:
            h = self.interrupt(aid)
            if h is not None:
                interrupted.append(aid)
                for cid in list(h.children_ids):
                    _cancel_rec(cid)

        _cancel_rec(agent_id)
        return interrupted


_registry = AgentRegistry()


def get_agent_registry() -> AgentRegistry:
    return _registry


def new_agent_id() -> str:
    return f"agent_{uuid.uuid4().hex[:8]}"


async def spawn_background_agent(
    manager: SubAgentManager,
    spec: SubAgentSpec,
    parent_agent_id: str | None = None,
) -> AgentHandle:
    registry = get_agent_registry()
    agent_id = new_agent_id()
    spec.agent_id = agent_id

    # Lineage propagation
    parent_handle = registry.get(parent_agent_id) if parent_agent_id else None
    if parent_handle:
        spec.parent_agent_id = parent_handle.id
        spec.root_agent_id = parent_handle.root_agent_id or parent_handle.id
        spec.depth = parent_handle.depth + 1
        if agent_id not in parent_handle.children_ids:
            parent_handle.children_ids.append(agent_id)
    elif spec.parent_agent_id:
        p_handle = registry.get(spec.parent_agent_id)
        if p_handle:
            spec.root_agent_id = p_handle.root_agent_id or p_handle.id
            spec.depth = p_handle.depth + 1
            if agent_id not in p_handle.children_ids:
                p_handle.children_ids.append(agent_id)
    else:
        spec.root_agent_id = agent_id

    handle = AgentHandle(
        id=agent_id,
        parent_session_id=spec.parent_session_id or "",
        description=spec.description,
        mode=spec.mode,
        spec=spec,
        parent_agent_id=spec.parent_agent_id,
        root_agent_id=spec.root_agent_id,
        depth=spec.depth,
        children_ids=[],
    )
    spec.handle = handle
    registry.register(handle)

    async def _run() -> None:
        try:
            result = await manager.spawn_subagent(spec)
            handle.result = result
            if handle.status != "interrupted":
                handle.status = result.status
        except asyncio.CancelledError:
            handle.status = "interrupted"
        except Exception as exc:
            handle.status = "failed"
            handle.result = SubAgentResult(
                task_id=spec.task_id,
                session_id="",
                status="failed",
                summary=str(exc),
                error=str(exc),
                parent_agent_id=spec.parent_agent_id,
                root_agent_id=spec.root_agent_id,
                depth=spec.depth,
                children_ids=list(spec.children_ids),
            )

    handle.task = asyncio.create_task(_run())
    return handle
