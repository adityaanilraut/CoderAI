"""Typed session-event system

The session log is an **append-only** sequence of typed ``SessionEvent``s.
LLM message history is *derived* from the log (``derive_messages()``), not
stored separately.  Every entry carries a monotonic ``seq``, an epoch-ms
``time``, and a ``type``-discriminated ``data`` payload.

Surface events contribute to model-visible history; log-only events are
persisted for audit / UI but never enter the LLM conversation window.
"""

from __future__ import annotations

import time as _time
import uuid
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Event type constants
# ---------------------------------------------------------------------------

# Turn / step lifecycle (log-only)
TURN_START = "turn/start"
TURN_END = "turn/end"
STEP_START = "step/start"
STEP_END = "step/end"

# Model-visible surface events
USER_MESSAGE = "user/message"
ASSISTANT_MESSAGE = "assistant/message"
TOOL_CALL = "tool/call"
TOOL_RESULT = "tool/result"

# Request header (log-only)
REQUEST_HEADER = "request/header"
REQUEST_CONTEXT = "request/context"

# Compaction (log-only except summary which is surface)
COMPACTION_START = "compaction/start"
COMPACTION_SUMMARY = "compaction/summary"
COMPACTION_END = "compaction/end"
COMPACTION_PRUNE = "compaction/prune"

# Steering (surface — injected context for next step)
STEERING_MESSAGE = "steering/message"

# Miscellaneous (log-only)
TODO_WRITE = "todo/write"
SESSION_END_SEED = "session/end-seed"

# Telemetry (log-only)
TELEMETRY_SPAN_START = "telemetry/span-start"
TELEMETRY_SPAN_END = "telemetry/span-end"
TELEMETRY_METRIC = "telemetry/metric"


# ---------------------------------------------------------------------------
# Surface / log-only classification
# ---------------------------------------------------------------------------

SURFACE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        USER_MESSAGE,
        ASSISTANT_MESSAGE,
        TOOL_CALL,
        TOOL_RESULT,
        COMPACTION_SUMMARY,
        STEERING_MESSAGE,
    }
)

LOG_ONLY_EVENT_TYPES: frozenset[str] = frozenset(
    {
        TURN_START,
        TURN_END,
        STEP_START,
        STEP_END,
        REQUEST_HEADER,
        REQUEST_CONTEXT,
        COMPACTION_START,
        COMPACTION_END,
        COMPACTION_PRUNE,
        TODO_WRITE,
        SESSION_END_SEED,
        TELEMETRY_SPAN_START,
        TELEMETRY_SPAN_END,
        TELEMETRY_METRIC,
    }
)


# ---------------------------------------------------------------------------
# SessionEvent — the universal log entry
# ---------------------------------------------------------------------------


@dataclass
class SessionEvent:
    """One entry in the append-only session log.

    ``seq`` is the monotonic position (``seq = log.length`` at append time).
    ``time`` is epoch milliseconds.  ``type`` discriminates ``data``.
    ``source_event_seqs`` optionally cites earlier events (e.g. compaction
    shadow references).
    """

    seq: int
    time: float  # epoch ms
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    source_event_seqs: list[int] | None = None

    # ------ derived helpers ------

    @property
    def is_surface(self) -> bool:
        return self.type in SURFACE_EVENT_TYPES

    @property
    def is_log_only(self) -> bool:
        return not self.is_surface

    # ------ serialisation ------

    def to_dict(self) -> dict[str, Any]:
        role_map = {
            USER_MESSAGE: "user" if self.data.get("source") != "system" else "system",
            ASSISTANT_MESSAGE: "assistant",
            TOOL_RESULT: "tool",
            TOOL_CALL: "tool_call",
            COMPACTION_SUMMARY: "system",
            STEERING_MESSAGE: "user",
        }
        d: dict[str, Any] = {
            "seq": self.seq,
            "time": self.time,
            "timestamp": int(self.time),
            "type": self.type,
            "role": role_map.get(self.type, self.type),
            "data": self.data,
        }
        if self.source_event_seqs:
            d["sourceEventSeqs"] = self.source_event_seqs
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SessionEvent:
        return cls(
            seq=d.get("seq", 0),
            time=d.get("time", 0.0),
            type=d.get("type", ""),
            data=d.get("data") or {},
            source_event_seqs=d.get("sourceEventSeqs"),
        )


# ---------------------------------------------------------------------------
# Event factory helpers
# ---------------------------------------------------------------------------


def _now_ms() -> float:
    return _time.time() * 1000


def _new_id() -> str:
    return uuid.uuid4().hex


def make_event(
    seq: int,
    event_type: str,
    data: dict[str, Any] | None = None,
    source_event_seqs: list[int] | None = None,
) -> SessionEvent:
    """Create a new ``SessionEvent`` with current timestamp."""
    return SessionEvent(
        seq=seq,
        time=_now_ms(),
        type=event_type,
        data=data or {},
        source_event_seqs=source_event_seqs,
    )


# ---- Turn / Step ----


def make_turn_start(seq: int, turn: int) -> SessionEvent:
    return make_event(seq, TURN_START, {"turn": turn})


def make_turn_end(seq: int, turn: int, reason: str) -> SessionEvent:
    return make_event(seq, TURN_END, {"turn": turn, "reason": reason})


def make_step_start(seq: int, turn: int, step: int) -> SessionEvent:
    return make_event(seq, STEP_START, {"turn": turn, "step": step})


def make_step_end(seq: int, turn: int, step: int) -> SessionEvent:
    return make_event(seq, STEP_END, {"turn": turn, "step": step})


# ---- User / Assistant / Tool ----


def make_user_message(
    seq: int,
    content: str,
    *,
    source: str = "user",
    message_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> SessionEvent:
    data: dict[str, Any] = {
        "id": message_id or _new_id(),
        "content": content,
        "source": source,
    }
    if meta:
        data["meta"] = meta
    return make_event(seq, USER_MESSAGE, data)


def make_assistant_message(
    seq: int,
    turn: int,
    step: int,
    *,
    content: str = "",
    thinking: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
    model: str | None = None,
    interrupted: bool = False,
    message_id: str | None = None,
) -> SessionEvent:
    data: dict[str, Any] = {
        "id": message_id or _new_id(),
        "turn": turn,
        "step": step,
        "content": content,
    }
    if thinking is not None:
        data["thinking"] = thinking
    if tool_calls:
        data["toolCalls"] = tool_calls
    if usage:
        data["usage"] = usage
    if model:
        data["model"] = model
    if interrupted:
        data["interrupted"] = True
    return make_event(seq, ASSISTANT_MESSAGE, data)


def make_tool_call(
    seq: int,
    turn: int,
    step: int,
    call_id: str,
    name: str,
    arguments: str,
) -> SessionEvent:
    return make_event(
        seq,
        TOOL_CALL,
        {
            "turn": turn,
            "step": step,
            "callId": call_id,
            "name": name,
            "arguments": arguments,
        },
    )


def make_tool_result(
    seq: int,
    turn: int,
    step: int,
    call_id: str,
    content: str,
    *,
    is_error: bool = False,
    meta: dict[str, Any] | None = None,
) -> SessionEvent:
    data: dict[str, Any] = {
        "turn": turn,
        "step": step,
        "callId": call_id,
        "content": content,
    }
    if is_error:
        data["isError"] = True
    if meta:
        data["meta"] = meta
    return make_event(seq, TOOL_RESULT, data)


# Convenience aliases matching event naming
make_user_event = make_user_message
make_assistant_event = make_assistant_message
make_tool_call_event = make_tool_call
make_tool_result_event = make_tool_result


# ---- Request header ----


def make_request_header(
    seq: int,
    header: dict[str, Any],
    reason: str = "initial",
) -> SessionEvent:
    return make_event(seq, REQUEST_HEADER, {"header": header, "reason": reason})


# ---- Compaction ----


def make_compaction_start(
    seq: int,
    compaction_id: str,
    shadowed_range: dict[str, int],
    trigger: str = "pressure",
) -> SessionEvent:
    return make_event(
        seq,
        COMPACTION_START,
        {
            "compactionId": compaction_id,
            "shadowedRange": shadowed_range,
            "trigger": trigger,
        },
    )


def make_compaction_summary(
    seq: int,
    compaction_id: str,
    content: str,
    shadowed_seqs: list[int] | None = None,
    shadowed_ids: list[str] | None = None,
) -> SessionEvent:
    data: dict[str, Any] = {
        "compactionId": compaction_id,
        "content": content,
        "shadowedSeqs": shadowed_seqs or [],
        "shadowedIds": shadowed_ids or [],
    }
    return make_event(
        seq,
        COMPACTION_SUMMARY,
        data,
        source_event_seqs=shadowed_seqs or [],
    )


def make_compaction_end(
    seq: int,
    compaction_id: str,
    shadowed_token_count: int = 0,
) -> SessionEvent:
    return make_event(
        seq,
        COMPACTION_END,
        {
            "compactionId": compaction_id,
            "shadowedTokenCount": shadowed_token_count,
        },
    )


# ---------------------------------------------------------------------------
# Legacy compat: convert old SessionMessage dicts to SessionEvent
# ---------------------------------------------------------------------------


def legacy_message_to_event(
    seq: int, msg_dict: dict[str, Any], session_id: str = ""
) -> SessionEvent:
    """Convert a legacy SessionMessage dict (from old JSONL) into a SessionEvent.

    This enables backward-compatible reading of pre-event-model session logs.
    """
    role = msg_dict.get("role", "")
    content = msg_dict.get("content", "")
    msg_id = msg_dict.get("id") or _new_id()
    meta = msg_dict.get("meta") or {}
    create_time = msg_dict.get("createTime", "")
    tool_calls = msg_dict.get("toolCalls")
    tool_call_id = msg_dict.get("toolCallId")
    thinking = msg_dict.get("thinking")

    # Parse epoch ms from ISO timestamp if available
    ts: float = _now_ms()
    if create_time:
        try:
            import datetime

            dt = datetime.datetime.fromisoformat(create_time)
            ts = dt.timestamp() * 1000
        except (ValueError, TypeError):
            pass

    if role == "system":
        # System messages become user/message with source=system
        return SessionEvent(
            seq=seq,
            time=ts,
            type=USER_MESSAGE,
            data={"id": msg_id, "content": content, "source": "system", "meta": meta},
        )
    elif role == "user":
        return SessionEvent(
            seq=seq,
            time=ts,
            type=USER_MESSAGE,
            data={"id": msg_id, "content": content, "source": "user", "meta": meta},
        )
    elif role == "assistant":
        data: dict[str, Any] = {
            "id": msg_id,
            "turn": 0,
            "step": 0,
            "content": content,
        }
        if thinking:
            data["thinking"] = thinking
        if tool_calls:
            data["toolCalls"] = tool_calls
        if meta:
            data["meta"] = meta
        return SessionEvent(seq=seq, time=ts, type=ASSISTANT_MESSAGE, data=data)
    elif role == "tool":
        return SessionEvent(
            seq=seq,
            time=ts,
            type=TOOL_RESULT,
            data={
                "turn": 0,
                "step": 0,
                "callId": tool_call_id or "",
                "content": content,
                "meta": meta,
            },
        )
    else:
        # Unknown role — treat as log-only user message
        return SessionEvent(
            seq=seq,
            time=ts,
            type=USER_MESSAGE,
            data={"id": msg_id, "content": content, "source": role, "meta": meta},
        )


# ---------------------------------------------------------------------------
# derive_messages() — project events into LLM-ready message list
# ---------------------------------------------------------------------------

MAX_TOOL_RESULT_CHARS = 32_000


def derive_messages_from_events(events: list[SessionEvent]) -> list[dict[str, Any]]:
    """Project the append-only event log into model-visible message history.

    1. Collect all shadowed seqs from compaction/summary events.
    2. Walk surface events, skip shadowed seqs.
    3. Truncate oversized tool results uniformly (cache-stable).
    """
    # Build the shadowed set from compaction summaries
    shadowed: set[int] = set()
    for ev in events:
        if ev.type == COMPACTION_SUMMARY:
            for s in ev.data.get("shadowedSeqs") or []:
                if isinstance(s, int):
                    shadowed.add(s)

    messages: list[dict[str, Any]] = []
    for ev in events:
        # Skip non-surface and shadowed events
        if not ev.is_surface or ev.seq in shadowed:
            continue

        if ev.type == USER_MESSAGE:
            messages.append(
                {
                    "role": "user" if ev.data.get("source") != "system" else "system",
                    "content": ev.data.get("content", ""),
                }
            )

        elif ev.type == ASSISTANT_MESSAGE:
            msg: dict[str, Any] = {
                "role": "assistant",
                "content": ev.data.get("content", ""),
            }
            if ev.data.get("toolCalls"):
                msg["tool_calls"] = ev.data["toolCalls"]
            if ev.data.get("thinking"):
                msg["reasoning_content"] = ev.data["thinking"]
            messages.append(msg)

        elif ev.type == TOOL_RESULT:
            content = ev.data.get("content", "")
            # Uniform truncation for cache stability
            if len(content) > MAX_TOOL_RESULT_CHARS:
                head = MAX_TOOL_RESULT_CHARS // 2
                tail = MAX_TOOL_RESULT_CHARS - head
                omitted = len(content) - MAX_TOOL_RESULT_CHARS
                content = (
                    f"{content[:head]}\n\n...[{omitted} characters omitted]...\n\n{content[-tail:]}"
                )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": ev.data.get("callId", ""),
                    "content": content,
                }
            )

        elif ev.type == COMPACTION_SUMMARY:
            # Compaction summary becomes a user message
            messages.append(
                {
                    "role": "user",
                    "content": ev.data.get("content", ""),
                }
            )

        elif ev.type == STEERING_MESSAGE:
            messages.append(
                {
                    "role": "user",
                    "content": ev.data.get("content", ""),
                }
            )

    return messages
