"""Session-level wire broadcast hub.

Out-of-turn publishers (approval runtime, notifications, background tasks)
publish here; each wire server holds one bounded live subscription. Stalled
streams fail explicitly instead of silently dropping messages.
"""

from __future__ import annotations

from typing import Any

from coderai.utils.broadcast import (
    BroadcastQueue,
    DEFAULT_SUBSCRIBER_QUEUE_LIMIT,
    SubscriptionQueue,
)


class RootWireHub(BroadcastQueue[Any]):
    """Route out-of-turn messages only to their owning conversation.

    Unscoped subscriptions are administrative observers. Session clients pass
    a nonempty identity and never receive events without a known owner.
    """

    def __init__(self, *, queue_limit: int = DEFAULT_SUBSCRIBER_QUEUE_LIMIT) -> None:
        super().__init__(history_limit=0, queue_limit=queue_limit)
        self._routes: dict[SubscriptionQueue[Any], str | None] = {}

    def subscribe(
        self, *, session_id: str | None = None, replay: bool = False
    ) -> SubscriptionQueue[Any]:
        if session_id is not None and (not isinstance(session_id, str) or not session_id.strip()):
            raise ValueError("Session subscriptions require a nonempty session_id")
        queue = super().subscribe(replay=False)
        if not queue.closed:
            self._routes[queue] = session_id
        return queue

    def unsubscribe(self, queue: SubscriptionQueue[Any]) -> None:
        super().unsubscribe(queue)
        self._routes.pop(queue, None)

    def _recipients(self, session_id: str | None) -> tuple[SubscriptionQueue[Any], ...]:
        return tuple(
            queue
            for queue, owner in self._routes.items()
            if owner is None or (session_id is not None and owner == session_id)
        )

    def publish_nowait(self, msg: Any, *, session_id: str | None = None) -> None:
        self._publish_to_nowait(msg, self._recipients(session_id))

    async def publish(self, msg: Any, *, session_id: str | None = None) -> None:
        await self._publish_to(msg, self._recipients(session_id))

    def shutdown(self, immediate: bool = True) -> None:
        super().shutdown(immediate=immediate)
        self._routes.clear()
