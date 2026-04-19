"""Centralized registry for tracking all active agents.

Provides real-time visibility into what each agent is doing,
its context/token usage, and a cooperative cancellation mechanism.
"""

import asyncio
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class AgentStatus(str, Enum):
    IDLE = "idle"
    THINKING = "thinking"
    TOOL_CALL = "tool_call"
    CANCELLED = "cancelled"
    DONE = "done"
    ERROR = "error"


@dataclass
class AgentInfo:
    """Snapshot of a tracked agent's state."""

    agent_id: str
    name: str
    role: Optional[str] = None
    parent_id: Optional[str] = None
    status: AgentStatus = AgentStatus.IDLE
    current_task: str = ""
    current_tool: Optional[str] = None
    model: str = ""

    # Token / cost accounting
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0

    # Context window
    context_used_tokens: int = 0
    context_limit_tokens: int = 0

    # Timing
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None

    # Cooperative cancellation
    _cancel_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    @property
    def elapsed_seconds(self) -> float:
        end = self.finished_at or time.time()
        return end - self.started_at

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def request_cancel(self):
        """Signal this agent to stop after the current step."""
        self._cancel_event.set()
        self.status = AgentStatus.CANCELLED

    @property
    def context_usage_pct(self) -> float:
        if self.context_limit_tokens <= 0:
            return 0.0
        return (self.context_used_tokens / self.context_limit_tokens) * 100


class AgentTracker:
    """Singleton registry that tracks every active agent."""

    def __init__(self):
        self._agents: Dict[str, AgentInfo] = {}
        self._lock = threading.Lock()

    def register(
        self,
        name: str = "main",
        role: Optional[str] = None,
        model: str = "",
        parent_id: Optional[str] = None,
        context_limit: int = 0,
    ) -> AgentInfo:
        """Register a new agent and return its info handle."""
        agent_id = f"agent_{uuid.uuid4().hex[:8]}"
        info = AgentInfo(
            agent_id=agent_id,
            name=name,
            role=role,
            model=model,
            parent_id=parent_id,
            context_limit_tokens=context_limit,
        )
        with self._lock:
            self._agents[agent_id] = info
        return info

    def get(self, agent_id: str) -> Optional[AgentInfo]:
        with self._lock:
            return self._agents.get(agent_id)

    def get_active(self) -> List[AgentInfo]:
        """Return all agents that are not yet done, cancelled, or errored."""
        with self._lock:
            return [
                a
                for a in list(self._agents.values())
                if a.status not in (AgentStatus.DONE, AgentStatus.ERROR, AgentStatus.CANCELLED)
            ]

    def get_all(self) -> List[AgentInfo]:
        with self._lock:
            return list(self._agents.values())

    def cancel_all(self):
        """Request cancellation for every active agent."""
        for info in self.get_active():
            info.request_cancel()

    def cancel(self, agent_id: str) -> bool:
        with self._lock:
            info = self._agents.get(agent_id)
            if info:
                info.request_cancel()
                # Recursively cancel all children
                children = [
                    child for child in list(self._agents.values())
                    if child.parent_id == agent_id
                    and child.status not in (AgentStatus.DONE, AgentStatus.ERROR, AgentStatus.CANCELLED)
                ]
            else:
                return False
        
        # Call cancel outside of lock to avoid recursive lock issues if not RLock
        for child in children:
            self.cancel(child.agent_id)
        return True


# Global singleton
agent_tracker = AgentTracker()
