"""Session-level wire broadcast hub.

Out-of-turn publishers (approval runtime, notifications, background tasks)
publish here; each wire server holds one subscription and forwards what it
sees. Sync fan-out over ``asyncio.Queue`` so publishers never block.
"""

from __future__ import annotations

import asyncio
from typing import Any


class RootWireHub:
    """Broadcast hub for out-of-turn wire messages."""

    def __init__(self) -> None:
        self._subs: list[asyncio.Queue] = []
        self._closed = False

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.append(q)
        return q

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        try:
            self._subs.remove(queue)
        except ValueError:
            pass

    def publish_nowait(self, msg: Any) -> None:
        if self._closed:
            return
        for q in list(self._subs):
            try:
                q.put_nowait(msg)
            except Exception:
                continue

    async def publish(self, msg: Any) -> None:
        self.publish_nowait(msg)

    def shutdown(self) -> None:
        self._closed = True
