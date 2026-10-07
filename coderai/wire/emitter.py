"""Session-scoped wire emission with a legacy process-wide fallback.

The core publishes lifecycle events here; UIs subscribe via ``get_emitter()``
or attach a session ``Wire``. When no session wire is attached, messages are
buffered (bounded) so ``--print --output-format stream-json`` can drain them.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import Future, TimeoutError
from collections import deque
from contextvars import ContextVar, Token
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    from coderai.wire import Wire, WireUISide
    from coderai.wire.types import WireMessage

_emitter_lock = threading.Lock()
_emitter: WireEmitter | None = None
_current_emitter: ContextVar[WireEmitter | None] = ContextVar("coderai_wire_emitter", default=None)


def bind_emitter(emitter: WireEmitter) -> Token[WireEmitter | None]:
    return _current_emitter.set(emitter)


def reset_emitter(token: Token[WireEmitter | None]) -> None:
    _current_emitter.reset(token)


def get_emitter() -> WireEmitter:
    current = _current_emitter.get()
    if current is not None:
        return current
    global _emitter
    with _emitter_lock:
        if _emitter is None:
            _emitter = WireEmitter()
        return _emitter


def wire_send(msg: WireMessage) -> None:
    """Publish a wire message from anywhere in the core."""
    get_emitter().send(msg)


class WireEmitter:
    """Fan-out hub: session wire passthrough + bounded replay buffer."""

    def __init__(self, buffer_size: int = 2000) -> None:
        from coderai.wire import Wire

        self._local = Wire()
        self._buffer: deque[WireMessage] = deque(maxlen=buffer_size)
        self._session_wire: Wire | None = None
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tool_results: set[str] = set()
        self._event_counts: dict[type, int] = {}
        self._closed = False

    # -- session attachment -------------------------------------------------
    def attach_session_wire(self, wire: Wire | None) -> None:
        with self._lock:
            self._session_wire = wire

    def detach_session_wire(self) -> None:
        with self._lock:
            self._session_wire = None

    def close(self) -> None:
        """Release subscriptions and retained session events on disposal."""
        with self._lock:
            self._closed = True
        try:
            self._local.shutdown(immediate=True)
        finally:
            with self._lock:
                self._session_wire = None
                self._buffer.clear()
                self._tool_results.clear()
                self._event_counts.clear()
            self._loop = None

    # -- publish -------------------------------------------------------------
    def send(self, msg: WireMessage) -> None:
        import asyncio

        with self._lock:
            if self._closed:
                return
            loop = self._loop

        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if loop is not None and loop.is_running() and current_loop is not loop:
            # Provider streaming runs in asyncio.to_thread. Asyncio queues
            # must be published from their owning loop, including failures.
            sent: Future[None] = Future()

            def publish() -> None:
                if not sent.set_running_or_notify_cancel():
                    return
                try:
                    self._send(msg)
                except BaseException as exc:
                    sent.set_exception(exc)
                else:
                    sent.set_result(None)

            loop.call_soon_threadsafe(publish)
            try:
                sent.result(timeout=5.0)
            except TimeoutError:
                sent.cancel()
                raise RuntimeError("Wire publisher did not acknowledge within 5 seconds") from None
            return
        self._send(msg)

    def _send(self, msg: WireMessage) -> None:
        from coderai.wire.types import ToolResult, TurnBegin

        with self._lock:
            if self._closed:
                return
            if isinstance(msg, TurnBegin):
                self._tool_results.clear()
            elif isinstance(msg, ToolResult):
                self._tool_results.add(msg.tool_call_id)
            self._buffer.append(msg)
            self._event_counts[type(msg)] = self._event_counts.get(type(msg), 0) + 1
            session_wire = self._session_wire
        self._local.soul_side.send(msg)
        if session_wire is not None:
            session_wire.soul_side.send(msg)

    def has_tool_result(self, tool_call_id: str) -> bool:
        with self._lock:
            return tool_call_id in self._tool_results

    def event_count(self, event_type: type) -> int:
        with self._lock:
            return self._event_counts.get(event_type, 0)

    def flush(self) -> None:
        self._local.soul_side.flush()
        with self._lock:
            session_wire = self._session_wire
        if session_wire is not None:
            session_wire.soul_side.flush()

    # -- subscribe ------------------------------------------------------------
    def ui_side(self, *, merge: bool, replay: bool = True, lossless: bool = False) -> WireUISide:
        import asyncio

        with self._lock:
            if self._closed:
                raise RuntimeError("Event stream is closed")

        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        return self._local.ui_side(merge=merge, replay=replay, lossless=lossless)

    def buffered(self) -> list[WireMessage]:
        with self._lock:
            return list(self._buffer)

    def clear_buffer(self) -> None:
        with self._lock:
            self._buffer.clear()

    # -- typed helpers --------------------------------------------------------
    def turn_begin(self, user_input: Any) -> None:
        from coderai.wire.types import TurnBegin

        self.send(TurnBegin(user_input=user_input))

    def turn_end(self) -> None:
        from coderai.wire.types import TurnEnd

        self.send(TurnEnd())

    def step_begin(self, n: int) -> None:
        from coderai.wire.types import StepBegin

        self.send(StepBegin(n=n))

    def step_interrupted(self) -> None:
        from coderai.wire.types import StepInterrupted

        self.send(StepInterrupted())

    def text(self, text: str) -> None:
        from coderai.wire.types import TextPart

        self.send(TextPart(text=text))

    def think(self, text: str) -> None:
        from coderai.wire.types import ThinkPart

        self.send(ThinkPart(text=text))

    def status(self, **kwargs: Any) -> None:
        from coderai.wire.types import StatusUpdate

        self.send(StatusUpdate(**kwargs))

    def btw_begin(self, question: str) -> str:
        from coderai.wire.types import BtwBegin

        bid = uuid.uuid4().hex[:8]
        self.send(BtwBegin(id=bid, question=question))
        return bid

    def btw_end(self, bid: str, response: str | None, error: str | None) -> None:
        from coderai.wire.types import BtwEnd

        self.send(BtwEnd(id=bid, response=response, error=error))

    def compaction_begin(self) -> None:
        from coderai.wire.types import CompactionBegin

        self.send(CompactionBegin())

    def compaction_end(self) -> None:
        from coderai.wire.types import CompactionEnd

        self.send(CompactionEnd())

    def mcp_loading_begin(self) -> None:
        from coderai.wire.types import MCPLoadingBegin

        self.send(MCPLoadingBegin())

    def mcp_loading_end(self) -> None:
        from coderai.wire.types import MCPLoadingEnd

        self.send(MCPLoadingEnd())

    def hook_triggered(self, event: str, target: str = "", count: int = 1) -> None:
        from coderai.wire.types import HookTriggered

        self.send(HookTriggered(event=event, target=target, hook_count=count))

    def hook_resolved(
        self, event: str, target: str = "", action: str = "allow", reason: str = ""
    ) -> None:
        from coderai.wire.types import HookResolved

        self.send(HookResolved(event=event, target=target, action=action, reason=reason))  # type: ignore[arg-type]

    async def drain_to_stream_json(self) -> list[dict[str, Any]]:
        """Serialize buffered messages as stream-json envelopes."""
        from coderai.wire.types import serialize_wire_message

        with self._lock:
            msgs = list(self._buffer)
        out: list[dict[str, Any]] = []
        for m in msgs:
            try:
                out.append(serialize_wire_message(m))
            except Exception:
                continue
        return out
