"""Lifecycle management, cascading cancellation trees, and structured process teardown."""

from coderai.core.lifecycle.cascade import CancellationTree, LifecycleCoordinator, get_lifecycle_coordinator
from coderai.core.lifecycle.teardown import TeardownCoordinator

__all__ = [
    "CancellationTree",
    "LifecycleCoordinator",
    "TeardownCoordinator",
    "get_lifecycle_coordinator",
]
