# Ported from coderai/core/* - kimi structure (telemetry/__init__.py).
from __future__ import annotations

import asyncio
import atexit
import time
import uuid
from collections import deque
from contextlib import suppress
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from coderai.telemetry.sink import (
    ExecutionSpan,
    MetricRecord,
    TelemetryCollector,
    get_telemetry_collector,
)

if TYPE_CHECKING:
    from coderai.telemetry.sink import EventSink

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
            asyncio.get_running_loop().create_task(_sink.flush())


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
]
