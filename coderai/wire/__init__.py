# Ported from coderai/core/wire/__init__.py - kimi structure (kimi_cli/wire/__init__.py).
"""Wire protocol (Kimi ``wire/`` parity).

SPMC event bus between the agent core ("soul side") and UIs ("UI side"),
with JSON-RPC 2.0 framing, ``wire.jsonl`` persistence + replay, and the full
Kimi message taxonomy (events + requests + envelopes).

Layout mirrors ``kimi_cli/wire/``:
- ``types.py`` — message models + envelope
- ``bus.py`` — in-process Soul/UI channel (merge + broadcast + recorder)
- ``jsonrpc.py`` — JSON-RPC 2.0 framing + error codes + statuses
- ``store.py`` — ``wire.jsonl`` persistence + replay (file backend)
- ``emitter.py`` — convenience ``wire_send`` facade used by the core
- ``serde.py`` — canonical serialize/deserialize re-exports
- ``hub.py`` — session-level broadcast hub (approval/notifications fan-out)
- ``server.py`` — stdio JSON-RPC server (``coderai --wire``)
"""

from coderai.core.wire.emitter import WireEmitter, get_emitter, wire_send
from coderai.wire.root_hub import RootWireHub
from coderai.wire.jsonrpc import ErrorCodes, Statuses
from coderai.wire.protocol import WIRE_PROTOCOL_LEGACY_VERSION, WIRE_PROTOCOL_VERSION
from coderai.wire.serde import deserialize_wire_message, serialize_wire_message
from coderai.wire.server import WireServer, run_wire_stdio
from coderai.wire.file import WireFile, WireMessageRecord, parse_wire_file_line
from coderai.wire.types import (
    EVENT_TYPES,
    REQUEST_TYPES,
    ApprovalRequest,
    ApprovalResponse,
    BtwBegin,
    BtwEnd,
    CompactionBegin,
    CompactionEnd,
    HookRequest,
    HookResponse,
    HookResolved,
    HookTriggered,
    MCPLoadingBegin,
    MCPLoadingEnd,
    MCPServerSnapshot,
    MCPStatusSnapshot,
    Notification,
    PlanDisplay,
    QuestionItem,
    QuestionNotSupported,
    QuestionOption,
    QuestionRequest,
    QuestionResponse,
    StatusUpdate,
    StepBegin,
    StepInterrupted,
    StepRetry,
    SteerInput,
    SubagentEvent,
    TextPart,
    ThinkPart,
    ToolCallPart,
    ToolCallRequest,
    ToolResultPart,
    TurnBegin,
    TurnEnd,
    WireMessageEnvelope,
    deserialize_wire_message,
    is_event,
    is_request,
    is_wire_message,
    serialize_wire_message,
)

__all__ = [
    "RootWireHub",
    "Wire",
    "WireSoulSide",
    "WireUISide",
    "WireEmitter",
    "get_emitter",
    "wire_send",
    "ErrorCodes",
    "Statuses",
    "WireFile",
    "WireMessageRecord",
    "WireServer",
    "parse_wire_file_line",
    "run_wire_stdio",
    "EVENT_TYPES",
    "REQUEST_TYPES",
    "ApprovalRequest",
    "ApprovalResponse",
    "BtwBegin",
    "BtwEnd",
    "CompactionBegin",
    "CompactionEnd",
    "HookRequest",
    "HookResponse",
    "HookTriggered",
    "HookResolved",
    "MCPLoadingBegin",
    "MCPLoadingEnd",
    "MCPServerSnapshot",
    "MCPStatusSnapshot",
    "Notification",
    "PlanDisplay",
    "QuestionItem",
    "QuestionNotSupported",
    "QuestionOption",
    "QuestionRequest",
    "QuestionResponse",
    "StatusUpdate",
    "StepBegin",
    "StepInterrupted",
    "StepRetry",
    "SteerInput",
    "SubagentEvent",
    "TextPart",
    "ThinkPart",
    "ToolCallPart",
    "ToolCallRequest",
    "ToolResultPart",
    "TurnBegin",
    "TurnEnd",
    "WIRE_PROTOCOL_LEGACY_VERSION",
    "WIRE_PROTOCOL_VERSION",
    "WireMessageEnvelope",
    "deserialize_wire_message",
    "is_event",
    "is_request",
    "is_wire_message",
    "serialize_wire_message",
]
# --- from coderai/core/wire/bus.py ---
"""In-process Wire bus (Kimi ``wire/__init__.py`` parity).

SPMC channel: one soul side publishes; N UI sides subscribe. Two streams:
- raw: every message verbatim
- merged: consecutive TextPart/ThinkPart chunks coalesced (streaming merge)

An optional ``WireFile`` backend records merged messages to ``wire.jsonl``.
"""


import asyncio
import copy
from collections import deque
from typing import Any

from coderai.wire.types import TextPart, ThinkPart, is_wire_message


class _Closed(Exception):
    pass


class _FanOut:
    """Minimal broadcast queue: each subscriber gets every item."""

    def __init__(self) -> None:
        self._subs: list[asyncio.Queue] = []
        self._closed = False

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.append(q)
        return q

    def publish_nowait(self, item: Any) -> None:
        if self._closed:
            raise _Closed("fanout closed")
        for q in list(self._subs):
            q.put_nowait(item)

    def shutdown(self) -> None:
        self._closed = True


class Wire:
    def __init__(self, *, file_backend: Any | None = None) -> None:
        self._raw = _FanOut()
        self._merged = _FanOut()
        self._soul_side = WireSoulSide(self._raw, self._merged)
        self._recorder: _WireRecorder | None = None
        if file_backend is not None:
            self._recorder = _WireRecorder(file_backend, self._merged.subscribe())

    @property
    def soul_side(self) -> WireSoulSide:
        return self._soul_side

    def ui_side(self, *, merge: bool) -> WireUISide:
        if merge:
            return WireUISide(self._merged.subscribe())
        return WireUISide(self._raw.subscribe())

    def shutdown(self) -> None:
        try:
            self.soul_side.flush()
        except Exception:
            pass
        self._raw.shutdown()
        self._merged.shutdown()

    async def join(self) -> None:
        if self._recorder is not None:
            await self._recorder.join()


class WireSoulSide:
    def __init__(self, raw: _FanOut, merged: _FanOut) -> None:
        self._raw = raw
        self._merged = merged
        self._merge_buffer: Any | None = None

    def send(self, msg: Any) -> None:
        if not is_wire_message(msg):
            raise ValueError(f"not a wire message: {type(msg)}")
        try:
            self._raw.publish_nowait(msg)
        except _Closed:
            return
        if isinstance(msg, (TextPart, ThinkPart)):
            if self._merge_buffer is None:
                self._merge_buffer = copy.deepcopy(msg)
            elif isinstance(self._merge_buffer, type(msg)):
                self._merge_buffer.text += msg.text
            else:
                self.flush()
                self._merge_buffer = copy.deepcopy(msg)
        else:
            self.flush()
            self._send_merged(msg)

    def flush(self) -> None:
        buf = self._merge_buffer
        if buf is None:
            return
        self._merge_buffer = None
        self._send_merged(buf)

    def _send_merged(self, msg: Any) -> None:
        try:
            self._merged.publish_nowait(msg)
        except _Closed:
            pass


class WireUISide:
    def __init__(self, queue: asyncio.Queue) -> None:
        self._queue = queue

    async def receive(self) -> Any:
        return await self._queue.get()


class _WireRecorder:
    def __init__(self, wire_file: Any, queue: asyncio.Queue) -> None:
        self._wire_file = wire_file
        self._queue = queue
        self._pending: deque[Any] = deque()
        self._task: asyncio.Task | None = None
        try:
            loop = asyncio.get_running_loop()
            self._task = loop.create_task(self._consume_loop())
        except RuntimeError:
            pass

    async def join(self) -> None:
        # Flush anything buffered synchronously.
        while self._pending:
            msg = self._pending.popleft()
            try:
                self._wire_file.append_message_sync(msg)
            except Exception:
                pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _consume_loop(self) -> None:
        while True:
            try:
                msg = await self._queue.get()
                try:
                    await asyncio.to_thread(self._wire_file.append_message_sync, msg)
                except Exception:
                    self._pending.append(msg)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(0.05)
