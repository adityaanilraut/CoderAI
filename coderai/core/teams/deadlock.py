"""Deadlock detection, task dependency cycle resolution, and watchdog mechanics."""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


class CycleDetectedError(Exception):
    """Raised when a circular dependency is detected in the task DAG."""

    def __init__(self, message: str, cycle_path: list[str] | None = None) -> None:
        super().__init__(message)
        self.cycle_path = cycle_path or []


class DeadlockError(Exception):
    """Raised when a circular wait or deadlock is detected across agent RPCs / delegations."""

    def __init__(self, message: str, wait_cycles: list[list[str]] | None = None) -> None:
        super().__init__(message)
        self.wait_cycles = wait_cycles or []


def detect_task_cycles(task_dependencies: dict[str, list[str]]) -> list[str] | None:
    """Detect circular dependencies in a task dependency map.

    Args:
        task_dependencies: Mapping of task_id -> list of dependency task_ids that must complete first.

    Returns:
        A list of task_ids representing the cycle path (e.g. ['A', 'B', 'C', 'A']), or None if acyclic.
    """
    # 0 = unvisited, 1 = visiting (in recursion stack), 2 = visited
    state: dict[str, int] = {node: 0 for node in task_dependencies}

    def _dfs(node: str, stack: list[str]) -> list[str] | None:
        state[node] = 1
        stack.append(node)

        deps = task_dependencies.get(node, [])
        for dep in deps:
            if dep not in state:
                # External or unlisted task dependency
                continue
            if state[dep] == 1:
                # Cycle found! Reconstruct cycle path from dep up to node
                try:
                    cycle_start = stack.index(dep)
                    cycle = stack[cycle_start:] + [dep]
                    return cycle
                except ValueError:
                    return [node, dep, node]
            elif state[dep] == 0:
                cycle = _dfs(dep, stack)
                if cycle is not None:
                    return cycle

        stack.pop()
        state[node] = 2
        return None

    for node in list(task_dependencies.keys()):
        if state[node] == 0:
            cycle = _dfs(node, [])
            if cycle is not None:
                return cycle

    return None


def assert_acyclic_dependencies(task_dependencies: dict[str, list[str]]) -> None:
    """Validate that task dependencies form a strict Directed Acyclic Graph (DAG).

    Raises:
        CycleDetectedError: if a cycle is found.
    """
    cycle = detect_task_cycles(task_dependencies)
    if cycle is not None:
        cycle_str = " -> ".join(cycle)
        raise CycleDetectedError(
            f"CycleDetectedError: Circular task dependency detected: {cycle_str}. "
            f"Tasks cannot depend on each other cyclically.",
            cycle_path=cycle,
        )


class InterAgentWaitWatchdog:
    """Tracks inter-agent delegation and RPC wait dependencies to prevent deadlocks."""

    def __init__(self) -> None:
        # waiter_id -> set of target_ids being awaited
        self._wait_graph: dict[str, set[str]] = {}
        self._timestamps: dict[tuple[str, str], float] = {}

    def record_wait(self, waiter_id: str, target_id: str) -> None:
        """Record that waiter_id is waiting on target_id."""
        if waiter_id == target_id:
            raise DeadlockError(
                f"SelfDeadlockError: Agent '{waiter_id}' cannot await itself.",
                wait_cycles=[[waiter_id, waiter_id]],
            )
        self._wait_graph.setdefault(waiter_id, set()).add(target_id)
        self._timestamps[(waiter_id, target_id)] = time.time()
        self.assert_no_deadlock()

    def release_wait(self, waiter_id: str, target_id: str) -> None:
        """Release wait relationship once delegation or RPC resolves."""
        if waiter_id in self._wait_graph:
            self._wait_graph[waiter_id].discard(target_id)
            if not self._wait_graph[waiter_id]:
                del self._wait_graph[waiter_id]
        self._timestamps.pop((waiter_id, target_id), None)

    def detect_circular_waits(self) -> list[list[str]]:
        """Find all circular wait cycles in the active wait graph."""
        dep_map = {waiter: list(targets) for waiter, targets in self._wait_graph.items()}
        cycle = detect_task_cycles(dep_map)
        return [cycle] if cycle else []

    def assert_no_deadlock(self) -> None:
        """Check for deadlocks and raise DeadlockError immediately if detected."""
        cycles = self.detect_circular_waits()
        if cycles:
            cycle_strs = [" -> ".join(c) for c in cycles]
            raise DeadlockError(
                f"DeadlockError: Circular inter-agent wait detected: {', '.join(cycle_strs)}. "
                f"Agents are mutually blocked awaiting each other.",
                wait_cycles=cycles,
            )
