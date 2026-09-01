"""Structured teardown coordinator and exception boundaries for CoderAI runtime."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from collections.abc import Callable

from coderai.core.lifecycle.cascade import get_lifecycle_coordinator

logger = logging.getLogger(__name__)


class TeardownCoordinator:
    """Coordinates graceful exit sequences, unhandled exception containment, and signal responses."""

    def __init__(self) -> None:
        self.is_tearing_down = False
        self._exit_callbacks: list[Callable[[], Any]] = []

    def register_exit_callback(self, callback: Callable[[], Any]) -> None:
        self._exit_callbacks.append(callback)

    async def execute_teardown(self, reason: str = "Teardown", exit_code: int = 0) -> None:
        """Execute complete cascade teardown and notify all registered exit callbacks."""
        if self.is_tearing_down:
            return
        self.is_tearing_down = True

        logger.debug("Executing system teardown: reason=%s, exit_code=%d", reason, exit_code)
        coord = get_lifecycle_coordinator()
        await coord.cancel_all(reason=reason)

        for cb in self._exit_callbacks:
            try:
                res = cb()
                if asyncio.iscoroutine(res):
                    await res
            except Exception as exc:
                logger.debug("Exit callback failed: %s", exc)


_global_teardown = TeardownCoordinator()


def get_teardown_coordinator() -> TeardownCoordinator:
    return _global_teardown
