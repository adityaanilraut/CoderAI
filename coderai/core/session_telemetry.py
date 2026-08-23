"""Session Telemetry & OpenTelemetry Integration — port of dsh session-telemetry and session-telemetry-otel.

Provides structured telemetry capture, handoff cursor tracking, redaction, and OpenTelemetry-compatible sinks.
"""

from __future__ import annotations

import abc
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from coderai.core.events import SessionEvent

logger = logging.getLogger(__name__)


class TelemetrySeverity(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"


class TelemetryMode(str, Enum):
    FULL = "FULL"
    FEEDBACK_ONLY = "FEEDBACK_ONLY"
    DISABLED = "DISABLED"


@dataclass
class SessionTelemetryRecord:
    """Standardized telemetry record emitted to sinks."""

    session_id: str
    record_type: str
    timestamp: float = field(default_factory=lambda: time.time() * 1000)
    severity: TelemetrySeverity = TelemetrySeverity.INFO
    attributes: dict[str, Any] = field(default_factory=dict)
    body: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "type": self.record_type,
            "timestamp": self.timestamp,
            "severity": self.severity.value,
            "attributes": self.attributes,
            "body": self.body,
        }

    def to_otel_log_record(self) -> dict[str, Any]:
        """Convert to standard OpenTelemetry log data model representation."""
        severity_number_map = {
            TelemetrySeverity.DEBUG: 5,
            TelemetrySeverity.INFO: 9,
            TelemetrySeverity.WARN: 13,
            TelemetrySeverity.ERROR: 17,
        }
        return {
            "Timestamp": int(self.timestamp * 1_000_000),  # nanoseconds
            "ObservedTimestamp": int(time.time() * 1_000_000_000),
            "SeverityNumber": severity_number_map.get(self.severity, 9),
            "SeverityText": self.severity.value,
            "Body": self.body or self.record_type,
            "Attributes": {
                **self.attributes,
                "session.id": self.session_id,
                "event.type": self.record_type,
            },
            "InstrumentationScope": {
                "name": "coderai.session.telemetry",
                "version": "0.4.0",
            },
        }


class SessionTelemetrySink(abc.ABC):
    """Abstract telemetry delivery sink."""

    @abc.abstractmethod
    def emit(self, record: SessionTelemetryRecord) -> None:
        """Deliver one telemetry record."""
        ...

    @abc.abstractmethod
    def flush(self) -> None:
        """Flush any pending buffered records."""
        ...

    @abc.abstractmethod
    def shutdown(self) -> None:
        """Close sink and release resources."""
        ...


class InMemoryTelemetrySink(SessionTelemetrySink):
    """In-memory sink useful for testing and audit trails."""

    def __init__(self) -> None:
        self.records: list[SessionTelemetryRecord] = []

    def emit(self, record: SessionTelemetryRecord) -> None:
        self.records.append(record)

    def flush(self) -> None:
        pass

    def shutdown(self) -> None:
        pass


class OTelStructuredTelemetrySink(SessionTelemetrySink):
    """OpenTelemetry-compatible structured sink with standard GenAI semantic attributes."""

    def __init__(self, mode: TelemetryMode = TelemetryMode.FULL) -> None:
        self.mode = mode
        self.emitted_records: list[dict[str, Any]] = []

    def emit(self, record: SessionTelemetryRecord) -> None:
        if self.mode == TelemetryMode.DISABLED:
            return
        if self.mode == TelemetryMode.FEEDBACK_ONLY and record.record_type != "feedback":
            return

        otel_repr = record.to_otel_log_record()
        self.emitted_records.append(otel_repr)
        logger.debug(f"[OTel Telemetry] {json.dumps(otel_repr)}")

    def flush(self) -> None:
        pass

    def shutdown(self) -> None:
        self.emitted_records.clear()


class SessionTelemetryCoordinator:
    """Coordinates session event observation, redaction, and telemetry emission.

    Port of @deepseek-ai/dsh-session-telemetry with handoff cursor tracking and
    lifecycle shutdown event guarantees.
    """

    def __init__(
        self,
        sink: SessionTelemetrySink,
        mode: TelemetryMode = TelemetryMode.FULL,
    ) -> None:
        self.sink = sink
        self.mode = mode
        self._handoff_cursors: dict[str, int] = {}
        self._adopted_sessions: set[str] = set()

    def adopt_session(self, session_id: str, metadata: dict[str, Any] | None = None) -> None:
        """Adopt a session into live telemetry tracking."""
        self._adopted_sessions.add(session_id)
        if session_id not in self._handoff_cursors:
            self._handoff_cursors[session_id] = 0

        self.sink.emit(
            SessionTelemetryRecord(
                session_id=session_id,
                record_type="session/created",
                severity=TelemetrySeverity.INFO,
                attributes={
                    "session.id": session_id,
                    **(metadata or {}),
                },
            )
        )

    def capture_event(self, session_id: str, event: SessionEvent) -> None:
        """Capture one session event with handoff cursor monotonic advance."""
        last_cursor = self._handoff_cursors.get(session_id, 0)
        if event.seq <= last_cursor and event.seq != 0:
            return

        self._handoff_cursors[session_id] = event.seq

        severity = TelemetrySeverity.INFO
        if "error" in event.type:
            severity = TelemetrySeverity.ERROR
        elif "warn" in event.type:
            severity = TelemetrySeverity.WARN

        # Redact sensitive fields if present
        attrs = dict(event.data) if isinstance(event.data, dict) else {"data": event.data}
        for sensitive_key in ("api_key", "password", "token", "secret"):
            if sensitive_key in attrs:
                attrs[sensitive_key] = "[REDACTED]"

        self.sink.emit(
            SessionTelemetryRecord(
                session_id=session_id,
                record_type=f"session/event/{event.type}",
                severity=severity,
                attributes={
                    "event.seq": event.seq,
                    "event.type": event.type,
                    **attrs,
                },
            )
        )

    def dispose_session(self, session_id: str) -> None:
        """Signal session disposal with operational shutdown marker."""
        if session_id in self._adopted_sessions:
            self._adopted_sessions.remove(session_id)
            self.sink.emit(
                SessionTelemetryRecord(
                    session_id=session_id,
                    record_type="session/disposed",
                    severity=TelemetrySeverity.INFO,
                    attributes={"session.id": session_id},
                )
            )

    def flush(self) -> None:
        self.sink.flush()

    def shutdown(self) -> None:
        for sid in list(self._adopted_sessions):
            self.dispose_session(sid)
        self.sink.shutdown()
