"""Continuable sub-agent control plane (list / send / interrupt) over SubAgentManager."""

from __future__ import annotations

import asyncio
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

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "mode": self.mode,
            "status": self.status,
            "inbox": len(self.inbox),
        }


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, AgentHandle] = {}

    def register(self, handle: AgentHandle) -> AgentHandle:
        self._agents[handle.id] = handle
        return handle

    def get(self, agent_id: str) -> AgentHandle | None:
        return self._agents.get(agent_id)

    def list(self, parent_session_id: str | None = None) -> list[AgentHandle]:
        items = list(self._agents.values())
        if parent_session_id:
            items = [a for a in items if a.parent_session_id == parent_session_id]
        return items

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


_registry = AgentRegistry()


def get_agent_registry() -> AgentRegistry:
    return _registry


def new_agent_id() -> str:
    return f"agent_{uuid.uuid4().hex[:8]}"


async def spawn_background_agent(
    manager: SubAgentManager,
    spec: SubAgentSpec,
) -> AgentHandle:
    agent_id = new_agent_id()
    handle = AgentHandle(
        id=agent_id,
        parent_session_id=spec.parent_session_id or "",
        description=spec.description,
        mode=spec.mode,
        spec=spec,
    )
    get_agent_registry().register(handle)

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
            )

    handle.task = asyncio.create_task(_run())
    return handle
