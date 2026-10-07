"""Task dependency cycle detection."""

from __future__ import annotations


class CycleDetectedError(Exception):
    """Raised when a circular dependency is detected in the task DAG."""

    def __init__(self, message: str, cycle_path: list[str] | None = None) -> None:
        super().__init__(message)
        self.cycle_path = cycle_path or []


def detect_task_cycles(task_dependencies: dict[str, list[str]]) -> list[str] | None:
    """Detect circular dependencies in a task dependency map.

    Args:
        task_dependencies: Mapping of task_id -> list of dependency task_ids that must complete first.

    Returns:
        A list of task_ids representing the cycle path (e.g. ['A', 'B', 'C', 'A']), or None if acyclic.
    """
    if len(task_dependencies) > 10000 or sum(map(len, task_dependencies.values())) > 100000:
        raise ValueError("Task graph exceeds supported size")
    state: dict[str, int] = {}
    for root in task_dependencies:
        if state.get(root):
            continue
        state[root] = 1
        path = [root]
        positions = {root: 0}
        stack = [(root, iter(task_dependencies[root]))]
        while stack:
            node, children = stack[-1]
            child = next(children, None)
            if child is None:
                state[node] = 2
                stack.pop()
                positions.pop(node)
                path.pop()
            elif child in task_dependencies:
                if state.get(child) == 1:
                    return path[positions[child] :] + [child]
                if not state.get(child):
                    state[child] = 1
                    positions[child] = len(path)
                    path.append(child)
                    stack.append((child, iter(task_dependencies[child])))
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
