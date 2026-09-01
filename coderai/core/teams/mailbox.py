"""Asynchronous actor mailbox and pub-sub communication channels for Agent Teams."""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MAILBOX_CAPACITY = 1000


class MessagePriority(enum.IntEnum):
    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3


@dataclass(order=True)
class PrioritizedMessage:
    """Wrapped message entry with priority sorting and monotonic arrival sequence."""

    priority_score: int
    arrival_time: float
    sequence: int
    payload: Any = field(compare=False)


class AsyncMailbox:
    """Bounded, thread-safe and async-safe FIFO/Priority mailbox for an agent actor."""

    def __init__(self, agent_id: str, max_size: int = DEFAULT_MAILBOX_CAPACITY) -> None:
        self.agent_id = agent_id
        self.max_size = max_size
        self._queue: asyncio.PriorityQueue[PrioritizedMessage] = asyncio.PriorityQueue(
            maxsize=max_size
        )
        self._seq = 0
        self._lock = asyncio.Lock()

    @property
    def size(self) -> int:
        return self._queue.qsize()

    def is_empty(self) -> bool:
        return self._queue.empty()

    def is_full(self) -> bool:
        return self._queue.full()

    async def send_async(
        self,
        message: Any,
        priority: MessagePriority = MessagePriority.NORMAL,
        timeout_seconds: float | None = None,
    ) -> bool:
        """Enqueue message asynchronously with priority and optional timeout."""
        self._seq += 1
        # Invert priority so higher enum values get popped first
        score = -int(priority)
        item = PrioritizedMessage(
            priority_score=score,
            arrival_time=time.time(),
            sequence=self._seq,
            payload=message,
        )

        try:
            if timeout_seconds is not None and timeout_seconds >= 0:
                await asyncio.wait_for(self._queue.put(item), timeout=timeout_seconds)
            else:
                await self._queue.put(item)
            return True
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning(
                "Mailbox send timed out for agent '%s' (capacity=%d)", self.agent_id, self.max_size
            )
            return False

    def send_nowait(
        self,
        message: Any,
        priority: MessagePriority = MessagePriority.NORMAL,
    ) -> bool:
        """Enqueue message immediately without blocking. Drops/rejects if full."""
        self._seq += 1
        score = -int(priority)
        item = PrioritizedMessage(
            priority_score=score,
            arrival_time=time.time(),
            sequence=self._seq,
            payload=message,
        )
        try:
            self._queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            logger.warning(
                "Mailbox queue full for agent '%s' (capacity=%d). Message dropped.",
                self.agent_id,
                self.max_size,
            )
            return False

    async def recv_async(self, timeout_seconds: float | None = None) -> Any:
        """Receive the next highest-priority message, waiting up to timeout_seconds."""
        if timeout_seconds is not None and timeout_seconds >= 0:
            item = await asyncio.wait_for(self._queue.get(), timeout=timeout_seconds)
        else:
            item = await self._queue.get()
        return item.payload

    def poll(self, max_items: int = 100) -> list[Any]:
        """Non-blocking drain of up to max_items pending messages."""
        collected: list[Any] = []
        while not self._queue.empty() and len(collected) < max_items:
            try:
                item = self._queue.get_nowait()
                collected.append(item.payload)
            except asyncio.QueueEmpty:
                break
        return collected


class ActorChannel:
    """Topic-based asynchronous pub-sub and broadcast event fabric for Agent Teams."""

    def __init__(self) -> None:
        self._topics: dict[str, set[AsyncMailbox]] = {}
        self._mailboxes: dict[str, AsyncMailbox] = {}
        self._lock = asyncio.Lock()

    def register_mailbox(self, agent_id: str, max_size: int = DEFAULT_MAILBOX_CAPACITY) -> AsyncMailbox:
        if agent_id not in self._mailboxes:
            self._mailboxes[agent_id] = AsyncMailbox(agent_id=agent_id, max_size=max_size)
        return self._mailboxes[agent_id]

    def get_mailbox(self, agent_id: str) -> AsyncMailbox | None:
        return self._mailboxes.get(agent_id)

    def subscribe(self, topic: str, mailbox: AsyncMailbox) -> None:
        subscribers = self._topics.setdefault(topic, set())
        subscribers.add(mailbox)

    def unsubscribe(self, topic: str, mailbox: AsyncMailbox) -> None:
        if topic in self._topics:
            self._topics[topic].discard(mailbox)
            if not self._topics[topic]:
                del self._topics[topic]

    async def publish_async(
        self,
        topic: str,
        message: Any,
        priority: MessagePriority = MessagePriority.NORMAL,
    ) -> int:
        """Publish a message to all subscribers of a specific topic."""
        subscribers = list(self._topics.get(topic, []))
        delivered = 0
        for sub in subscribers:
            ok = await sub.send_async(message, priority=priority)
            if ok:
                delivered += 1
        return delivered

    async def broadcast_async(
        self,
        message: Any,
        priority: MessagePriority = MessagePriority.NORMAL,
        exclude_agent_id: str | None = None,
    ) -> int:
        """Broadcast a message to all registered agent mailboxes."""
        delivered = 0
        for aid, mb in self._mailboxes.items():
            if exclude_agent_id and aid == exclude_agent_id:
                continue
            ok = await mb.send_async(message, priority=priority)
            if ok:
                delivered += 1
        return delivered
