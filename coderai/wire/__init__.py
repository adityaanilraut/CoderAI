from __future__ import annotations

import asyncio
import contextlib
import copy

from kosong.message import MergeableMixin

from coderai.utils.aioqueue import QueueShutDown
from coderai.utils.broadcast import (
    BroadcastQueue,
    BroadcastQueueOverflow,
    DEFAULT_HISTORY_LIMIT,
    DEFAULT_SUBSCRIBER_QUEUE_LIMIT,
    SubscriptionQueue,
)
from coderai.utils.logging import logger
from coderai.wire.file import WireFile
from coderai.wire.types import ContentPart, ToolCallPart, WireMessage, is_wire_message

WireMessageQueue = BroadcastQueue[WireMessage]


class Wire:
    """
    A spmc channel for communication between the soul and the UI during a soul run.
    """

    def __init__(
        self,
        *,
        file_backend: WireFile | None = None,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
        queue_limit: int = DEFAULT_SUBSCRIBER_QUEUE_LIMIT,
    ):
        self._raw_queue = WireMessageQueue(history_limit=history_limit, queue_limit=queue_limit)
        self._merged_queue = WireMessageQueue(history_limit=history_limit, queue_limit=queue_limit)

        self._soul_side = WireSoulSide(self._raw_queue, self._merged_queue)

        self._recorder: _WireRecorder | None
        if file_backend is not None:
            self._recorder = _WireRecorder(file_backend, self._merged_queue.subscribe(replay=False))
        else:
            self._recorder = None

    @property
    def soul_side(self) -> WireSoulSide:
        return self._soul_side

    def ui_side(self, *, merge: bool, replay: bool = True, lossless: bool = False) -> WireUISide:
        """
        Create a UI side of the `Wire`.

        Args:
            merge: Whether to merge `Wire` messages as much as possible.
        """
        if merge:
            return WireUISide(self._merged_queue.subscribe(replay=replay, lossless=lossless))
        else:
            return WireUISide(self._raw_queue.subscribe(replay=replay, lossless=lossless))

    def shutdown(self, *, immediate: bool = False) -> None:
        try:
            if not immediate:
                self.soul_side.flush()
        finally:
            logger.debug("Shutting down wire")
            self.soul_side._closed = True
            self.soul_side._merge_buffer = None
            self._raw_queue.shutdown(immediate=immediate)
            self._merged_queue.shutdown(immediate=immediate)

    async def join(self) -> None:
        if self._recorder is None:
            return
        try:
            await self._recorder.join()
        except BroadcastQueueOverflow:
            raise
        except Exception:
            logger.exception("Wire recorder failed to flush:")


class WireSoulSide:
    """
    The soul side of a `Wire`.
    """

    def __init__(self, raw_queue: WireMessageQueue, merged_queue: WireMessageQueue):
        self._raw_queue = raw_queue
        self._merged_queue = merged_queue
        self._merge_buffer: MergeableMixin | None = None
        self._closed = False

    def send(self, msg: WireMessage) -> None:
        if self._closed:
            return
        if not isinstance(msg, ContentPart | ToolCallPart):
            logger.debug("Sending wire message: {msg}", msg=msg)

        # send raw message
        try:
            self._raw_queue.publish_nowait(msg)
        except QueueShutDown:
            logger.info("Failed to send raw wire message, queue is shut down: {msg}", msg=msg)
            self._closed = True
            self._merge_buffer = None
            return

        # merge and send merged message
        match msg:
            case MergeableMixin():
                if self._merge_buffer is None:
                    self._merge_buffer = copy.deepcopy(msg)
                elif self._merge_buffer.merge_in_place(msg):
                    pass
                else:
                    self.flush()
                    self._merge_buffer = copy.deepcopy(msg)
            case _:
                self.flush()
                self._send_merged(msg)

    def flush(self) -> None:
        if self._closed:
            self._merge_buffer = None
            return
        buffer = self._merge_buffer
        if buffer is None:
            return
        assert is_wire_message(buffer)
        self._send_merged(buffer)
        self._merge_buffer = None

    def _send_merged(self, msg: WireMessage) -> None:
        try:
            self._merged_queue.publish_nowait(msg)
        except QueueShutDown:
            logger.info("Failed to send merged wire message, queue is shut down: {msg}", msg=msg)


class WireUISide:
    """
    The UI side of a `Wire`.
    """

    def __init__(self, queue: SubscriptionQueue[WireMessage]):
        self._queue = queue

    def close(self) -> None:
        self._queue.close()

    async def aclose(self) -> None:
        self.close()

    def __enter__(self) -> WireUISide:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    async def __aenter__(self) -> WireUISide:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.close()

    async def receive(self) -> WireMessage:
        msg = await self._queue.get()
        if not isinstance(msg, ContentPart | ToolCallPart):
            logger.debug("Receiving wire message: {msg}", msg=msg)
        return msg

    def try_receive_nowait(self) -> WireMessage | None:
        """Return the next queued message without blocking, or ``None`` if empty."""
        try:
            return self._queue.get_nowait()
        except (asyncio.QueueEmpty, QueueShutDown):
            return None

    def drain_nowait(self) -> int:
        """Discard already-queued messages and return how many were dropped.

        `BroadcastQueue.subscribe` replays its history, so a per-turn subscriber
        would otherwise re-emit everything published by earlier turns.
        """
        dropped = 0
        while self.try_receive_nowait() is not None:
            dropped += 1
        return dropped


class _WireRecorder:
    def __init__(self, wire_file: WireFile, queue: SubscriptionQueue[WireMessage]) -> None:
        self._wire_file = wire_file
        self._task = asyncio.create_task(self._consume_loop(queue))
        self._task.add_done_callback(lambda _: queue.close())

    async def join(self) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    async def _consume_loop(self, queue: SubscriptionQueue[WireMessage]) -> None:
        try:
            while True:
                try:
                    msg = await queue.get()
                    await self._record(msg)
                except QueueShutDown:
                    break
        finally:
            queue.close()

    async def _record(self, msg: WireMessage) -> None:
        await self._wire_file.append_message(msg)
