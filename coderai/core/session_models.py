"""Session message and entry models, serialization, and usage tracking."""

from __future__ import annotations

import datetime
import time
import uuid
from dataclasses import dataclass
from typing import Any

from coderai.core.common.usage import accumulate_usage_dict


def _now() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


@dataclass
class SessionMessage:
    """An individual message or event row within a session."""

    id: str
    session_id: str
    role: str  # system | user | assistant | tool
    content: str = ""
    tool_calls: list[Any] | None = None
    tool_call_id: str | None = None
    thinking: str | None = None
    compacted: bool = False
    visible: bool = True
    create_time: str = ""
    update_time: str = ""
    meta: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sessionId": self.session_id,
            "role": self.role,
            "content": self.content,
            "toolCalls": self.tool_calls,
            "toolCallId": self.tool_call_id,
            "thinking": self.thinking,
            "compacted": self.compacted,
            "visible": self.visible,
            "createTime": self.create_time,
            "updateTime": self.update_time,
            "meta": self.meta,
        }


@dataclass
class SessionEntry:
    """Summary and status of a session stored in the sessions index."""

    id: str
    summary: str = ""
    assistant_reply: str | None = None
    assistant_thinking: str | None = None
    assistant_refusal: str | None = None
    tool_calls: list[Any] | None = None
    status: str = "pending"
    fail_reason: str | None = None
    ask_permissions: list[dict[str, Any]] | None = None
    processes: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    usage_per_model: dict[str, Any] | None = None
    active_tokens: int = 0
    create_time: str = ""
    update_time: str = ""
    plan_mode: bool = False
    fork_of: str | None = None
    parent_session_id: str | None = None
    fork_point: str | int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "summary": self.summary,
            "assistantReply": self.assistant_reply,
            "assistantThinking": self.assistant_thinking,
            "assistantRefusal": self.assistant_refusal,
            "toolCalls": self.tool_calls,
            "status": self.status,
            "failReason": self.fail_reason,
            "askPermissions": self.ask_permissions,
            "processes": self.processes,
            "usage": self.usage,
            "usagePerModel": self.usage_per_model,
            "activeTokens": self.active_tokens,
            "createTime": self.create_time,
            "updateTime": self.update_time,
            "planMode": self.plan_mode,
            "forkOf": self.fork_of,
            "parentSessionId": self.parent_session_id or self.fork_of,
            "forkPoint": self.fork_point,
        }


def serialize_message(m: SessionMessage) -> dict[str, Any]:
    """Serialize a SessionMessage instance into a persistent dictionary format."""
    ts = 0
    if m.create_time:
        try:
            ts = int(datetime.datetime.fromisoformat(m.create_time).timestamp() * 1000)
        except Exception:
            ts = int(time.time() * 1000)
    else:
        ts = int(time.time() * 1000)
    return {
        "id": m.id,
        "sessionId": m.session_id,
        "role": m.role,
        "content": m.content,
        "toolCalls": m.tool_calls,
        "toolCallId": m.tool_call_id,
        "thinking": m.thinking,
        "compacted": m.compacted,
        "visible": m.visible,
        "createTime": m.create_time,
        "updateTime": m.update_time,
        "timestamp": ts,
        "meta": m.meta,
    }


def deserialize_message(d: dict[str, Any], session_id: str) -> SessionMessage | None:
    """Deserialize a dictionary or typed SessionEvent into a SessionMessage."""
    from coderai.core.events import (
        LOG_ONLY_EVENT_TYPES,
        USER_MESSAGE,
        ASSISTANT_MESSAGE,
        TOOL_RESULT,
        COMPACTION_SUMMARY,
        STEERING_MESSAGE,
    )

    event_type = d.get("type")
    if event_type:
        if event_type in LOG_ONLY_EVENT_TYPES:
            return None
        data = d.get("data") or {}
        time_val = d.get("time") or d.get("timestamp") or 0.0
        create_time = ""
        if time_val:
            try:
                create_time = datetime.datetime.fromtimestamp(
                    float(time_val) / 1000.0, tz=datetime.timezone.utc
                ).isoformat()
            except Exception:
                create_time = _now()
        if event_type == USER_MESSAGE:
            return SessionMessage(
                id=data.get("id") or uuid.uuid4().hex,
                session_id=session_id,
                role="user" if data.get("source") != "system" else "system",
                content=data.get("content") or "",
                create_time=create_time or _now(),
                update_time=create_time or _now(),
                meta=data.get("meta"),
            )
        elif event_type == ASSISTANT_MESSAGE:
            return SessionMessage(
                id=data.get("id") or uuid.uuid4().hex,
                session_id=session_id,
                role="assistant",
                content=data.get("content") or "",
                tool_calls=data.get("toolCalls"),
                thinking=data.get("thinking"),
                create_time=create_time or _now(),
                update_time=create_time or _now(),
                meta=data.get("meta"),
            )
        elif event_type == TOOL_RESULT:
            return SessionMessage(
                id=uuid.uuid4().hex,
                session_id=session_id,
                role="tool",
                content=data.get("content") or "",
                tool_call_id=data.get("callId"),
                create_time=create_time or _now(),
                update_time=create_time or _now(),
                meta=data.get("meta"),
            )
        elif event_type == COMPACTION_SUMMARY:
            return SessionMessage(
                id=uuid.uuid4().hex,
                session_id=session_id,
                role="system",
                content=f"There are earlier parts of the conversation. Here is a summary:\n\n{data.get('content', '')}",
                create_time=create_time or _now(),
                update_time=create_time or _now(),
                meta={
                    "isSummary": True,
                    "kind": "compact/summary",
                    "replacedIds": data.get("shadowedIds", []),
                },
                visible=False,
            )
        elif event_type == STEERING_MESSAGE:
            return SessionMessage(
                id=data.get("id") or uuid.uuid4().hex,
                session_id=session_id,
                role="user",
                content=data.get("content") or "",
                create_time=create_time or _now(),
                update_time=create_time or _now(),
                meta=data.get("meta"),
            )
        return None

    # Legacy SessionMessage dict
    create_time = d.get("createTime") or ""
    if not create_time and d.get("timestamp"):
        try:
            create_time = datetime.datetime.fromtimestamp(
                float(d["timestamp"]) / 1000.0, tz=datetime.timezone.utc
            ).isoformat()
        except Exception:
            create_time = _now()
    return SessionMessage(
        id=d.get("id") or uuid.uuid4().hex,
        session_id=session_id,
        role=d.get("role") or "user",
        content=d.get("content") or "",
        tool_calls=d.get("toolCalls"),
        tool_call_id=d.get("toolCallId"),
        thinking=d.get("thinking"),
        compacted=bool(d.get("compacted")),
        visible=d.get("visible") is not False,
        create_time=create_time or _now(),
        update_time=d.get("updateTime") or create_time or _now(),
        meta=d.get("meta"),
    )


def entry_from_dict(d: dict[str, Any]) -> SessionEntry:
    """Instantiate a SessionEntry from an index entry dictionary."""
    return SessionEntry(
        id=d.get("id", ""),
        summary=d.get("summary", ""),
        assistant_reply=d.get("assistantReply"),
        assistant_thinking=d.get("assistantThinking"),
        assistant_refusal=d.get("assistantRefusal"),
        tool_calls=d.get("toolCalls"),
        status=d.get("status", "pending"),
        fail_reason=d.get("failReason"),
        ask_permissions=d.get("askPermissions"),
        processes=d.get("processes"),
        usage=d.get("usage"),
        usage_per_model=d.get("usagePerModel"),
        active_tokens=d.get("activeTokens", 0),
        create_time=d.get("createTime", ""),
        update_time=d.get("updateTime", ""),
        plan_mode=bool(d.get("planMode")),
        fork_of=d.get("forkOf") or d.get("parentSessionId"),
        parent_session_id=d.get("parentSessionId") or d.get("forkOf"),
        fork_point=d.get("forkPoint"),
    )


def accumulate_usage(
    current: dict[str, Any] | None, usage: dict[str, Any] | None
) -> dict[str, Any] | None:
    return accumulate_usage_dict(current, usage)


def accumulate_usage_per_model(
    current: dict[str, Any] | None, model: str, usage: dict[str, Any] | None
) -> dict[str, Any] | None:
    if usage is None or not model:
        return current
    res = dict(current or {})
    res[model] = accumulate_usage(res.get(model), usage)
    return res


def total_tokens(usage: dict[str, Any] | None) -> int:
    return usage.get("total_tokens", 0) if usage else 0


def copy_message_with(m: SessionMessage, **changes: Any) -> SessionMessage:
    return SessionMessage(
        id=m.id,
        session_id=m.session_id,
        role=m.role,
        content=m.content,
        tool_calls=m.tool_calls,
        tool_call_id=m.tool_call_id,
        thinking=m.thinking,
        compacted=changes.get("compacted", m.compacted),
        visible=m.visible,
        create_time=m.create_time,
        update_time=changes.get("update_time", m.update_time),
        meta=m.meta,
    )
