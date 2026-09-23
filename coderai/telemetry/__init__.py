from __future__ import annotations

import asyncio
import atexit
import os
import time
import uuid
from collections import deque
from contextlib import suppress
from contextvars import ContextVar
from typing import Any

from coderai.telemetry.sink import (
    ExecutionSpan,
    MetricRecord,
    TelemetryCollector,
    get_telemetry_collector,
)


# ---------------------------------------------------------------------------
# Module-level state (zero dependencies)
# ---------------------------------------------------------------------------

_MAX_QUEUE_SIZE = 1000
_event_queue: deque[dict[str, Any]] = deque(maxlen=_MAX_QUEUE_SIZE)
_device_id: str | None = None
_session_id: str | None = None
_client_info: tuple[str, str | None] | None = None
_session_started_sessions: set[str] = set()
_sink: Any | None = None
_disabled: bool = False
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def _track_background_task(task: asyncio.Task[Any]) -> None:
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


_trace_id_var: ContextVar[str | None] = ContextVar("coderai_telemetry_trace_id", default=None)


def set_current_trace_id(trace_id: str | None) -> None:
    """Record the trace id of the latest LLM request in the current context."""
    _trace_id_var.set(trace_id)


def get_current_trace_id() -> str | None:
    """The trace id of the latest LLM request in the current task context."""
    return _trace_id_var.get()


def set_context(*, device_id: str, session_id: str) -> None:
    """Set device and session identifiers. Call once after app init."""
    global _device_id, _session_id
    _device_id = device_id
    _session_id = session_id


def set_client_info(*, name: str, version: str | None = None) -> None:
    """Set the wire/acp client name and version."""
    global _client_info
    if not name:
        return
    _client_info = (name, version)


def get_client_info() -> tuple[str, str | None] | None:
    """Return the current (name, version) tuple, or None if unset."""
    return _client_info


def track_session_started_once(
    *,
    ui_mode: str,
    resumed: bool,
    client_name: str | None = None,
    client_version: str | None = None,
) -> None:
    """Emit one session_started event for current session."""
    session_id = _session_id
    if not session_id or session_id in _session_started_sessions:
        return

    ui = (ui_mode or "unknown").strip().lower()
    name = client_name
    version = client_version
    if name is None and ui in {"wire", "acp"}:
        client_info = get_client_info()
        if client_info is not None:
            name, version = client_info
    if not name:
        name = ui or "unknown"

    _session_started_sessions.add(session_id)
    track(
        "session_started",
        client_name=name,
        client_version=version,
        ui_mode=ui,
        resumed=resumed,
    )

    if _sink is not None:
        with suppress(Exception):
            t = asyncio.get_running_loop().create_task(_sink.flush())
            _track_background_task(t)


def disable() -> None:
    """Permanently disable telemetry for this process."""
    global _disabled
    _disabled = True
    _event_queue.clear()
    if _sink is not None:
        _sink.clear_buffer()


def attach_sink(sink: Any) -> None:
    """Attach the event sink and drain any queued events."""
    global _sink
    if _sink is not None and _sink is not sink:
        with suppress(Exception):
            _sink.flush_sync()
    _sink = sink
    if _event_queue:
        for event in _event_queue:
            if event.get("device_id") is None:
                event["device_id"] = _device_id
            if event.get("session_id") is None:
                event["session_id"] = _session_id
            _sink.accept(event)
        _event_queue.clear()


def track(event: str, **properties: bool | int | float | str | None) -> None:
    """Record a telemetry event."""
    if _disabled:
        return

    record = {
        "event_id": uuid.uuid4().hex,
        "device_id": _device_id,
        "session_id": _session_id,
        "event": event,
        "timestamp": time.time(),
        "properties": properties if properties else {},
    }

    if _sink is not None:
        _sink.accept(record)
    else:
        _event_queue.append(record)


def get_sink() -> Any | None:
    """Return current sink, or None if not attached."""
    return _sink


def flush_sync() -> None:
    """Synchronously flush any buffered events. Called on exit."""
    if _sink is not None:
        _sink.flush_sync()


class TransportSink:
    """Adapt ``AsyncTransport.send`` to the ``accept`` / ``flush`` sink API."""

    def __init__(self, transport: Any) -> None:
        self._transport = transport
        self._pending: list[dict[str, Any]] = []

    def accept(self, event: dict[str, Any]) -> None:
        self._pending.append(event)
        if len(self._pending) >= 25:
            self.flush_sync()

    def clear_buffer(self) -> None:
        self._pending.clear()

    def _take(self) -> list[dict[str, Any]]:
        batch = self._pending
        self._pending = []
        return batch

    async def flush(self) -> None:
        batch = self._take()
        if batch:
            await self._transport.send(batch)

    def flush_sync(self) -> None:
        batch = self._take()
        if not batch:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self._transport.send(batch))
            return
        t = loop.create_task(self._transport.send(batch))
        _track_background_task(t)


def telemetry_requested(settings: dict[str, Any] | None) -> bool:
    """Opt-in gate. Environment ``CODERAI_TELEMETRY`` overrides settings."""
    raw = os.getenv("CODERAI_TELEMETRY", "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    if not isinstance(settings, dict):
        return False
    flag = settings.get("telemetryEnabled", settings.get("telemetry"))
    if isinstance(flag, bool):
        return flag
    if isinstance(flag, str):
        return flag.strip().lower() in {"1", "true", "yes", "on"}
    return False


def apply_telemetry_policy(settings: dict[str, Any] | None) -> None:
    """Attach the HTTP sink when telemetry is enabled; otherwise drop events."""
    global _disabled
    # The test runner must not upload events unless a test opts in.
    if os.getenv("PYTEST_CURRENT_TEST") and os.getenv(
        "CODERAI_TELEMETRY", ""
    ).strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        _disabled = True
        _event_queue.clear()
        return
    if not telemetry_requested(settings):
        _disabled = True
        _event_queue.clear()
        return
    _disabled = False
    if get_sink() is not None:
        return
    from coderai.auth.oauth import _device_id
    from coderai.telemetry.transport import AsyncTransport

    device_id = _device_id()
    set_context(device_id=device_id, session_id=_session_id or "")
    attach_sink(TransportSink(AsyncTransport(device_id=device_id)))


atexit.register(flush_sync)

__all__ = [
    "ExecutionSpan",
    "MetricRecord",
    "TelemetryCollector",
    "get_telemetry_collector",
    "track",
    "set_current_trace_id",
    "get_current_trace_id",
    "set_context",
    "set_client_info",
    "get_client_info",
    "track_session_started_once",
    "disable",
    "attach_sink",
    "get_sink",
    "flush_sync",
    "TransportSink",
    "telemetry_requested",
    "apply_telemetry_policy",
]
