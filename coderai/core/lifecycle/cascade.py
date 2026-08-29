"""Cascading cancellation tree and lifecycle coordination across tasks and subprocesses."""

from __future__ import annotations

import asyncio
import inspect
import logging
import signal
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from coderai.core.common.process_tree import escalated_kill_process_tree, is_process_alive

logger = logging.getLogger(__name__)


@dataclass
class CancellationNode:
    """A node in the hierarchical execution cancellation tree."""

    id: str
    parent_id: str | None = None
    children: set[str] = field(default_factory=set)
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    pids: set[int] = field(default_factory=set)
    cleanup_callbacks: list[Callable[[], Any]] = field(default_factory=list)


class CancellationTree:
    """Hierarchical cancellation tree mapping parent sessions to child turns, subagents, and processes."""

    def __init__(self) -> None:
        self._nodes: dict[str, CancellationNode] = {}
        self._lock = asyncio.Lock()

    def register_node(self, node_id: str, parent_id: str | None = None) -> CancellationNode:
        """Register or retrieve a node in the cancellation hierarchy."""
        if node_id not in self._nodes:
            self._nodes[node_id] = CancellationNode(id=node_id, parent_id=parent_id)
        node = self._nodes[node_id]
        if parent_id and node.parent_id != parent_id:
            node.parent_id = parent_id

        if parent_id:
            if parent_id not in self._nodes:
                self._nodes[parent_id] = CancellationNode(id=parent_id)
            self._nodes[parent_id].children.add(node_id)
        return node

    def register_task(
        self, node_id: str, task: asyncio.Task[Any], parent_id: str | None = None
    ) -> None:
        """Attach an active asyncio.Task to a specific node."""
        node = self.register_node(node_id, parent_id)
        node.tasks.add(task)

    def register_process(self, node_id: str, pid: int, parent_id: str | None = None) -> None:
        """Attach a spawned OS subprocess PID to a specific node."""
        node = self.register_node(node_id, parent_id)
        node.pids.add(pid)

    def register_cleanup(
        self, node_id: str, callback: Callable[[], Any], parent_id: str | None = None
    ) -> None:
        """Register a synchronous or asynchronous cleanup hook to run on cancellation."""
        node = self.register_node(node_id, parent_id)
        node.cleanup_callbacks.append(callback)

    def unregister_task(self, node_id: str, task: asyncio.Task[Any]) -> None:
        if node_id in self._nodes:
            self._nodes[node_id].tasks.discard(task)

    def unregister_process(self, node_id: str, pid: int) -> None:
        if node_id in self._nodes:
            self._nodes[node_id].pids.discard(pid)

    def unregister_node(self, node_id: str) -> None:
        node = self._nodes.pop(node_id, None)
        if node and node.parent_id and node.parent_id in self._nodes:
            self._nodes[node.parent_id].children.discard(node_id)

    async def cancel_subtree(
        self,
        node_id: str,
        reason: str = "Interrupted/Cancelled",
        grace_seconds: float = 0.5,
    ) -> dict[str, Any]:
        """Recursively cancel a node and all of its descendants, terminating subprocesses and tasks."""
        cancelled_tasks_count = 0
        killed_pids: list[int] = []
        visited_nodes: list[str] = []

        async def _cancel_rec(nid: str) -> None:
            if nid not in self._nodes:
                return
            node = self._nodes[nid]
            visited_nodes.append(nid)

            # 1. Recurse down all children first (bottom-up propagation)
            for child_id in list(node.children):
                await _cancel_rec(child_id)

            # 2. Cancel all attached asyncio tasks
            nonlocal cancelled_tasks_count
            for t in list(node.tasks):
                if not t.done():
                    t.cancel()
                    cancelled_tasks_count += 1

            # 3. Kill all registered OS subprocesses using 3-stage escalated teardown
            for pid in list(node.pids):
                if is_process_alive(pid):
                    escalated_kill_process_tree(
                        pid, int_grace_sec=grace_seconds, term_grace_sec=grace_seconds
                    )
                    killed_pids.append(pid)

            # 4. Run cleanup handlers
            for cb in node.cleanup_callbacks:
                try:
                    if inspect.iscoroutinefunction(cb):
                        await cb()
                    else:
                        res = cb()
                        if inspect.isawaitable(res):
                            await res
                except Exception as exc:
                    logger.debug("Cleanup callback failed for node '%s': %s", nid, exc)

            # Remove node from registry
            self.unregister_node(nid)

        await _cancel_rec(node_id)
        return {
            "root_node": node_id,
            "visited_nodes": visited_nodes,
            "cancelled_tasks": cancelled_tasks_count,
            "killed_pids": killed_pids,
            "reason": reason,
        }


class LifecycleCoordinator:
    """Coordinates cascading cancellation and process management for the orchestrator."""

    def __init__(self) -> None:
        self.tree = CancellationTree()
        self._active_sessions: set[str] = set()

    def register_session(self, session_id: str) -> None:
        self._active_sessions.add(session_id)
        self.tree.register_node(session_id)

    def unregister_session(self, session_id: str) -> None:
        self._active_sessions.discard(session_id)
        self.tree.unregister_node(session_id)

    async def cancel_session_cascade(self, session_id: str, reason: str = "SIGINT") -> dict[str, Any]:
        """Trigger cascading cancellation for a session and all its child tasks/subagents."""
        return await self.tree.cancel_subtree(session_id, reason=reason)

    async def cancel_all(self, reason: str = "Global Teardown") -> list[dict[str, Any]]:
        """Cancel all registered active sessions."""
        results: list[dict[str, Any]] = []
        for sid in list(self._active_sessions):
            res = await self.cancel_session_cascade(sid, reason=reason)
            results.append(res)
        return results


_global_coordinator = LifecycleCoordinator()


def get_lifecycle_coordinator() -> LifecycleCoordinator:
    """Return singleton LifecycleCoordinator instance."""
    return _global_coordinator
