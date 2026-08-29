"""Telemetry and streaming event middleware interceptor chain for CoderAI."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExecutionSpan:
    """Represents a discrete unit of work / execution span (OpenTelemetry-compatible)."""

    span_id: str
    trace_id: str
    name: str
    kind: str = "internal"  # "internal" | "tool" | "llm" | "subagent" | "session" | "compaction"
    parent_span_id: str | None = None
    start_time_ms: float = field(default_factory=lambda: time.time() * 1000.0)
    end_time_ms: float | None = None
    duration_ms: float = 0.0
    status: str = "running"  # "running" | "ok" | "error"
    error: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    def finish(
        self,
        status: str = "ok",
        error: str | None = None,
        extra_attributes: dict[str, Any] | None = None,
    ) -> None:
        self.end_time_ms = time.time() * 1000.0
        self.duration_ms = max(0.0, self.end_time_ms - self.start_time_ms)
        self.status = status
        if error is not None:
            self.error = error
            self.status = "error"
        if extra_attributes:
            self.attributes.update(extra_attributes)

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        self.events.append(
            {
                "name": name,
                "timestamp_ms": time.time() * 1000.0,
                "attributes": attributes or {},
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "trace_id": self.trace_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "kind": self.kind,
            "start_time_ms": self.start_time_ms,
            "end_time_ms": self.end_time_ms,
            "duration_ms": round(self.duration_ms, 2),
            "status": self.status,
            "error": self.error,
            "attributes": dict(self.attributes),
            "events": list(self.events),
        }

    def to_otel_span(self) -> dict[str, Any]:
        """Convert to OpenTelemetry-compatible span dict structure."""
        return {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "parentSpanId": self.parent_span_id,
            "name": self.name,
            "kind": self.kind.upper(),
            "startTimeUnixNano": int(self.start_time_ms * 1_000_000),
            "endTimeUnixNano": int((self.end_time_ms or self.start_time_ms) * 1_000_000),
            "status": {"code": 1 if self.status == "ok" else 2, "message": self.error or ""},
            "attributes": [
                {"key": k, "value": {"stringValue": str(v)}} for k, v in self.attributes.items()
            ],
            "events": [
                {
                    "timeUnixNano": int(e["timestamp_ms"] * 1_000_000),
                    "name": e["name"],
                    "attributes": [
                        {"key": k, "value": {"stringValue": str(v)}}
                        for k, v in e.get("attributes", {}).items()
                    ],
                }
                for e in self.events
            ],
        }


@dataclass
class MetricRecord:
    name: str
    value: float
    tags: dict[str, str] = field(default_factory=dict)
    timestamp_ms: float = field(default_factory=lambda: time.time() * 1000.0)


class TelemetryCollector:
    """Thread-safe collector for execution spans, metrics counters, and duration tracing."""

    def __init__(self) -> None:
        self._spans: dict[str, ExecutionSpan] = {}
        self._metrics: list[MetricRecord] = []
        self._counters: dict[str, float] = {}
        self._active_trace_id: str = uuid.uuid4().hex[:16]

    def set_active_trace_id(self, trace_id: str) -> None:
        self._active_trace_id = trace_id

    def get_active_trace_id(self) -> str:
        return self._active_trace_id

    def start_span(
        self,
        name: str,
        kind: str = "internal",
        parent_span_id: str | None = None,
        attributes: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> ExecutionSpan:
        """Create and begin tracking a new execution span."""
        span_id = uuid.uuid4().hex[:16]
        effective_trace_id = trace_id or self._active_trace_id
        span = ExecutionSpan(
            span_id=span_id,
            trace_id=effective_trace_id,
            name=name,
            kind=kind,
            parent_span_id=parent_span_id,
            attributes=dict(attributes or {}),
        )
        self._spans[span_id] = span
        return span

    def end_span(
        self,
        span_id: str,
        status: str = "ok",
        error: str | None = None,
        extra_attributes: dict[str, Any] | None = None,
    ) -> ExecutionSpan | None:
        """Complete an existing span and update duration and status."""
        span = self._spans.get(span_id)
        if not span:
            return None
        span.finish(status=status, error=error, extra_attributes=extra_attributes)
        return span

    def record_metric(
        self,
        metric_name: str,
        value: float = 1.0,
        tags: dict[str, str] | None = None,
    ) -> None:
        """Record a metric data point."""
        rec = MetricRecord(name=metric_name, value=value, tags=tags or {})
        self._metrics.append(rec)
        self._counters[metric_name] = self._counters.get(metric_name, 0.0) + value

    def increment_counter(
        self,
        name: str,
        amount: float = 1.0,
        tags: dict[str, str] | None = None,
    ) -> float:
        """Increment a named counter and return the updated total."""
        self.record_metric(name, amount, tags=tags)
        return self._counters.get(name, 0.0)

    def get_metrics_summary(self) -> dict[str, Any]:
        """Return aggregated summary of metric counters and span totals."""
        total_spans = len(self._spans)
        ok_spans = sum(1 for s in self._spans.values() if s.status == "ok")
        error_spans = sum(1 for s in self._spans.values() if s.status == "error")
        avg_duration = (
            sum(s.duration_ms for s in self._spans.values() if s.end_time_ms is not None)
            / max(1, sum(1 for s in self._spans.values() if s.end_time_ms is not None))
        )

        return {
            "total_spans": total_spans,
            "ok_spans": ok_spans,
            "error_spans": error_spans,
            "avg_span_duration_ms": round(avg_duration, 2),
            "counters": dict(self._counters),
        }

    def export_spans(
        self,
        trace_id: str | None = None,
        as_otel: bool = False,
    ) -> list[dict[str, Any]]:
        """Export all recorded spans, optionally filtered by trace ID."""
        spans = list(self._spans.values())
        if trace_id:
            spans = [s for s in spans if s.trace_id == trace_id]
        if as_otel:
            return [s.to_otel_span() for s in spans]
        return [s.to_dict() for s in spans]

    def clear(self) -> None:
        """Reset all spans and metrics."""
        self._spans.clear()
        self._metrics.clear()
        self._counters.clear()
        self._active_trace_id = uuid.uuid4().hex[:16]


_global_telemetry = TelemetryCollector()


def get_telemetry_collector() -> TelemetryCollector:
    return _global_telemetry
