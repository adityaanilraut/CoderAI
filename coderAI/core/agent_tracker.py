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
from typing import AbstractSet, Any, Dict, Iterable, List, Optional, cast


class AgentStatus(str, Enum):
    IDLE = "idle"
    THINKING = "thinking"
    TOOL_CALL = "tool_call"
    WAITING_FOR_USER = "waiting_for_user"
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

    def request_cancel(self) -> None:
        """Signal this agent to stop after the current step."""
        self._cancel_event.set()
        self.status = AgentStatus.CANCELLED
        self.finished_at = time.time()


# Upper bound on how many finished agent records we retain in memory.
# Long-running interactive chats with many delegations would otherwise grow
# ``_agents`` without bound. Active agents are never pruned.
_MAX_FINISHED_AGENTS = 200


class AgentTracker:
    """Singleton registry that tracks every active agent.

    Finished agents are retained in ``_agents`` so UIs can show last-known
    state, but the number of retained *finished* entries is capped at
    ``_MAX_FINISHED_AGENTS`` (LRU by ``finished_at``); active entries are
    never evicted.
    """

    def __init__(self) -> None:
        self._agents: Dict[str, AgentInfo] = {}
        # threading.Lock is intentionally used here rather than asyncio.Lock.
        # The critical sections only perform dict lookups/insertions which take
        # microseconds, so the event loop is never blocked for any meaningful
        # duration.  Do NOT add any ``await`` inside a ``with self._lock`` block
        # or call any coroutine that might block; that would stall the loop.
        self._lock = threading.RLock()

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
            self._prune_finished_locked()
        return info

    def _prune_finished_locked(self) -> None:
        """Evict the oldest finished agents when we exceed the retention cap.

        Caller must hold ``self._lock``. Children reference parents via
        ``parent_id`` for the UI tree view, so a parent is only evicted once
        it has no living children still in the map.
        """
        finished = [
            a
            for a in self._agents.values()
            if a.status in (AgentStatus.DONE, AgentStatus.ERROR, AgentStatus.CANCELLED)
        ]
        if len(finished) <= _MAX_FINISHED_AGENTS:
            return
        # Oldest first; tolerate ``finished_at=None`` by treating it as 0.
        finished.sort(key=lambda a: a.finished_at or 0.0)
        to_evict = len(finished) - _MAX_FINISHED_AGENTS
        live_parent_ids = {
            a.parent_id
            for a in self._agents.values()
            if a.parent_id
            and a.status not in (AgentStatus.DONE, AgentStatus.ERROR, AgentStatus.CANCELLED)
        }
        for a in finished:
            if to_evict <= 0:
                break
            if a.agent_id in live_parent_ids:
                continue
            self._agents.pop(a.agent_id, None)
            to_evict -= 1

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

    def cancel_all(self) -> None:
        """Request cancellation for every active agent."""
        with self._lock:
            for info in self._agents.values():
                if info.status not in (AgentStatus.DONE, AgentStatus.ERROR, AgentStatus.CANCELLED):
                    info.request_cancel()

    def cancel(self, agent_id: str) -> bool:
        with self._lock:
            root = self._agents.get(agent_id)
            if not root:
                return False
            # Build a parent → live-children map once so we descend the tree in
            # O(n) instead of rescanning ``self._agents`` per recursive hop.
            terminal = (AgentStatus.DONE, AgentStatus.ERROR, AgentStatus.CANCELLED)
            children_by_parent: Dict[str, List[AgentInfo]] = {}
            for info in self._agents.values():
                if info.parent_id:
                    children_by_parent.setdefault(info.parent_id, []).append(info)
            # Iterative DFS — cheap and avoids re-entering the lock per child.
            stack: List[AgentInfo] = [root]
            visited: set[str] = set()
            while stack:
                cur = stack.pop()
                if cur.agent_id in visited:
                    continue
                visited.add(cur.agent_id)
                stack.extend(children_by_parent.get(cur.agent_id, []))
                if cur.status in terminal:
                    continue
                cur.request_cancel()
            return True

    def clear_except(self, keep_ids: Optional[Iterable[str]] = None) -> None:
        """Remove tracked agents except those in *keep_ids*."""
        keep: AbstractSet[str] = set(keep_ids or [])
        with self._lock:
            for aid in list(self._agents.keys()):
                if aid not in keep:
                    self._agents.pop(aid, None)


def get_agent_tracker() -> "AgentTracker":
    """Resolve the active agent tracker (process-shared via ToolServices)."""
    from coderAI.core.services import get_services

    return get_services().agent_tracker


class _LazyAgentTracker:
    """Module-level proxy delegating to the active ToolServices agent tracker.

    Kept so existing ``from coderAI.core.agent_tracker import agent_tracker``
    import sites (agent, bridge controller/serializers, tui session setup)
    keep working after ownership moved into ToolServices. Tests that patch
    ``coderAI.bridge.controller.agent_tracker`` still rebind that module's
    own name; everything else resolves through the process-wide default
    container, so all references observe the same tracker by default.
    """

    def __getattr__(self, name: str) -> Any:
        return getattr(get_agent_tracker(), name)

    def __repr__(self) -> str:
        return repr(get_agent_tracker())


# Backward-compat alias — lazily delegates to the active container's tracker.
agent_tracker: "AgentTracker" = cast("AgentTracker", _LazyAgentTracker())
