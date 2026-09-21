from __future__ import annotations

import asyncio

from typing import Generic, TypeVar

from coderai.utils.aioqueue import Queue, QueueShutDown

T = TypeVar("T")


class BroadcastQueue(Generic[T]):
    """
    A broadcast queue that allows multiple subscribers to receive published items.
    """

    def __init__(self) -> None:
        self._queues: set[Queue[T]] = set()
        self._history: list[T] = []
        self._is_shutdown: bool = False

    def subscribe(self) -> Queue[T]:
        """Create a new subscription queue."""
        queue: Queue[T] = Queue()
        for item in self._history:
            queue.put_nowait(item)
        if self._is_shutdown:
            queue.shutdown()
        else:
            self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: Queue[T]) -> None:
        """Remove a subscription queue."""
        self._queues.discard(queue)

    async def publish(self, item: T) -> None:
        """Publish an item to all subscription queues."""
        if self._is_shutdown:
            raise QueueShutDown
        self._history.append(item)
        await asyncio.gather(*(queue.put(item) for queue in self._queues))

    def publish_nowait(self, item: T) -> None:
        """Publish an item to all subscription queues without waiting."""
        if self._is_shutdown:
            raise QueueShutDown
        self._history.append(item)
        for queue in list(self._queues):
            queue.put_nowait(item)

    def shutdown(self, immediate: bool = False) -> None:
        """Close all subscription queues."""
        self._is_shutdown = True
        for queue in list(self._queues):
            queue.shutdown(immediate=immediate)
        self._queues.clear()
