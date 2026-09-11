"""Process-global wire emitter (Kimi ``soul.wire_send`` parity).

The core publishes lifecycle events here; UIs subscribe via ``get_emitter()``
or attach a session ``Wire``. When no session wire is attached, messages are
buffered (bounded) so ``--print --output-format stream-json`` can drain them.
"""

from __future__ import annotations

import threading
import uuid
from collections import deque
from typing import Any

_emitter_lock = threading.Lock()
_emitter: WireEmitter | None = None


def get_emitter() -> WireEmitter:
    global _emitter
    with _emitter_lock:
        if _emitter is None:
            _emitter = WireEmitter()
        return _emitter


def wire_send(msg: Any) -> None:
    """Publish a wire message from anywhere in the core (Kimi ``wire_send``)."""
    try:
        get_emitter().send(msg)
    except Exception:
        pass


class WireEmitter:
    """Fan-out hub: session wire passthrough + bounded replay buffer."""

    def __init__(self, buffer_size: int = 2000) -> None:
        from coderai.wire import Wire

        self._local = Wire()
        self._buffer: deque[Any] = deque(maxlen=buffer_size)
        self._session_wire: Any | None = None
        self._lock = threading.Lock()

    # -- session attachment -------------------------------------------------
    def attach_session_wire(self, wire: Any | None) -> None:
        with self._lock:
            self._session_wire = wire

    def detach_session_wire(self) -> None:
        with self._lock:
            self._session_wire = None

    # -- publish -------------------------------------------------------------
    def send(self, msg: Any) -> None:
        with self._lock:
            self._buffer.append(msg)
            session_wire = self._session_wire
        try:
            self._local.soul_side.send(msg)
        except Exception:
            pass
        if session_wire is not None:
            try:
                session_wire.soul_side.send(msg)
            except Exception:
                pass

    def flush(self) -> None:
        try:
            self._local.soul_side.flush()
        except Exception:
            pass
        with self._lock:
            session_wire = self._session_wire
        if session_wire is not None:
            try:
                session_wire.soul_side.flush()
            except Exception:
                pass

    # -- subscribe ------------------------------------------------------------
    def ui_side(self, *, merge: bool) -> Any:
        return self._local.ui_side(merge=merge)

    def buffered(self) -> list[Any]:
        with self._lock:
            return list(self._buffer)

    def clear_buffer(self) -> None:
        with self._lock:
            self._buffer.clear()

    # -- typed helpers (Kimi soul event parity) --------------------------------
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
