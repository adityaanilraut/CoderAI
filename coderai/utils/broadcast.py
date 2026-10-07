from __future__ import annotations

import asyncio
from collections import deque
from typing import Generic, TypeVar

from coderai.utils.aioqueue import Queue, QueueShutDown

T = TypeVar("T")

DEFAULT_HISTORY_LIMIT = 512
DEFAULT_SUBSCRIBER_QUEUE_LIMIT = 4096


class BroadcastQueueOverflow(RuntimeError):
    """A live subscriber fell behind; delivery failed instead of dropping silently."""


class SubscriptionQueue(Queue[T], Generic[T]):
    """Bounded subscription with an explicit, idempotent lifetime."""

    def __init__(self, owner: BroadcastQueue[T], maxsize: int, *, lossless: bool = True) -> None:
        super().__init__(maxsize=maxsize)
        self._owner = owner
        self.lossless = lossless
        self._closed = False
        self._failure: BroadcastQueueOverflow | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        self._owner.unsubscribe(self)

    async def aclose(self) -> None:
        self.close()

    def __enter__(self) -> SubscriptionQueue[T]:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    async def __aenter__(self) -> SubscriptionQueue[T]:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.close()

    def _detach(self, *, immediate: bool) -> None:
        self._closed = True
        self.shutdown(immediate=immediate)

    def _fail(self, error: BroadcastQueueOverflow) -> None:
        self._failure = error
        self.close()

    async def get(self) -> T:
        if self._failure is not None:
            raise self._failure
        try:
            return await super().get()
        except QueueShutDown:
            if self._failure is not None:
                raise self._failure from None
            raise

    def get_nowait(self) -> T:
        if self._failure is not None:
            raise self._failure
        return super().get_nowait()


class BroadcastQueue(Generic[T]):
    """Bounded replay with lossless active delivery or an explicit overflow error.

    Replay retains the most recent ``history_limit`` items. Live subscribers
    have bounded queues: async publishing waits for capacity; synchronous
    publishing disconnects a stalled subscriber and reports failure to both
    producer and consumer. Healthy subscribers still receive the item.
    """

    def __init__(
        self,
        *,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
        queue_limit: int = DEFAULT_SUBSCRIBER_QUEUE_LIMIT,
    ) -> None:
        if history_limit < 0 or queue_limit < 1 or history_limit > queue_limit:
            raise ValueError("Require 0 <= history_limit <= queue_limit and queue_limit > 0")
        self._queues: set[SubscriptionQueue[T]] = set()
        self._history: deque[T] = deque(maxlen=history_limit)
        self._queue_limit = queue_limit
        self._is_shutdown = False

    @property
    def subscriber_count(self) -> int:
        return len(self._queues)

    @property
    def history_size(self) -> int:
        return len(self._history)

    def subscribe(self, *, replay: bool = True, lossless: bool = True) -> SubscriptionQueue[T]:
        queue = SubscriptionQueue(self, self._queue_limit, lossless=lossless)
        if replay:
            for item in self._history:
                queue.put_nowait(item)
        if self._is_shutdown:
            queue._detach(immediate=False)
        else:
            self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: SubscriptionQueue[T]) -> None:
        self._queues.discard(queue)
        queue._detach(immediate=True)

    async def publish(self, item: T) -> None:
        await self._publish_to(item, tuple(self._queues))

    async def _publish_to(self, item: T, queues: tuple[SubscriptionQueue[T], ...]) -> None:
        if self._is_shutdown:
            raise QueueShutDown
        self._history.append(item)

        async def _put(queue: SubscriptionQueue[T]) -> None:
            try:
                await queue.put(item)
            except QueueShutDown:
                # Closing a subscription deliberately stops its delivery.
                self._queues.discard(queue)

        await asyncio.gather(*(_put(queue) for queue in queues))

    def publish_nowait(self, item: T) -> None:
        self._publish_to_nowait(item, tuple(self._queues))

    def _publish_to_nowait(self, item: T, queues: tuple[SubscriptionQueue[T], ...]) -> None:
        if self._is_shutdown:
            raise QueueShutDown
        self._history.append(item)
        overflow: BroadcastQueueOverflow | None = None
        for queue in queues:
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                error = BroadcastQueueOverflow(
                    f"Subscriber exceeded its {self._queue_limit}-message queue; stream disconnected."
                )
                queue._fail(error)
                if queue.lossless:
                    overflow = error
            except QueueShutDown:
                self._queues.discard(queue)
        if overflow is not None:
            raise overflow

    def shutdown(self, immediate: bool = False) -> None:
        self._is_shutdown = True
        for queue in tuple(self._queues):
            queue._detach(immediate=immediate)
        self._queues.clear()
        self._history.clear()
