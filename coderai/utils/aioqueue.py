from __future__ import annotations

import asyncio
import sys
from typing import Any, Generic, TypeVar

T = TypeVar("T")

if sys.version_info >= (3, 13):
    QueueShutDown = asyncio.QueueShutDown  # type: ignore[assignment]

    class Queue(asyncio.Queue[T], Generic[T]):
        """Asyncio Queue with shutdown support."""

else:

    class QueueShutDown(Exception):
        """Raised when operating on a shut down queue."""

    class Queue(asyncio.Queue[Any], Generic[T]):
        """Asyncio Queue with shutdown support for Python < 3.13."""

        def __init__(self, maxsize: int = 0) -> None:
            super().__init__(maxsize=maxsize)
            self._shutdown = False

        def shutdown(self, immediate: bool = False) -> None:
            if self._shutdown and not immediate:
                return
            self._shutdown = True
            if immediate:
                while self._queue:
                    self._get()
                    if self._unfinished_tasks > 0:
                        self._unfinished_tasks -= 1
                if self._unfinished_tasks == 0:
                    self._finished.set()
            # Inserting a sentinel into a full bounded queue would discard
            # live messages on graceful close. Wake operations explicitly.
            for waiters in (self._getters, self._putters):
                while waiters:
                    waiter = waiters.popleft()
                    if not waiter.done():
                        waiter.set_exception(QueueShutDown())

        async def get(self) -> T:
            if self._shutdown and self.empty():
                raise QueueShutDown
            return await super().get()

        def get_nowait(self) -> T:
            if self._shutdown and self.empty():
                raise QueueShutDown
            return super().get_nowait()

        async def put(self, item: T) -> None:
            if self._shutdown:
                raise QueueShutDown
            await super().put(item)

        def put_nowait(self, item: T) -> None:
            if self._shutdown:
                raise QueueShutDown
            super().put_nowait(item)
