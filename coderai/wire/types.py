"""Wire message taxonomy.

Dataclass-based (no pydantic dependency): events + requests + typed envelope
with ``{"type": ..., "payload": {...}}`` framing. Request objects carry an
asyncio future so the core can ``await request.wait()`` for UI resolution.
"""

from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass, field
import datetime
from enum import Enum
import pathlib
from typing import Any, Literal, Union

from kosong.message import (
    AudioURLPart as AudioURLPart,
    ContentPart as ContentPart,
    ImageURLPart as ImageURLPart,
    ToolCall as ToolCall,
    VideoURLPart as VideoURLPart,
)
from kosong.tooling import (
    BriefDisplayBlock as BriefDisplayBlock,
    DisplayBlock as DisplayBlock,
    ToolResult as ToolResult,
    ToolReturnValue as ToolReturnValue,
    UnknownDisplayBlock as UnknownDisplayBlock,
)
from coderai.tools.display import (
    BackgroundTaskDisplayBlock as BackgroundTaskDisplayBlock,
    DiffDisplayBlock as DiffDisplayBlock,
    ShellDisplayBlock as ShellDisplayBlock,
    TodoDisplayBlock as TodoDisplayBlock,
    TodoDisplayItem as TodoDisplayItem,
)


@dataclass
class TextPart:
    text: str
    TYPE: str = "text"


@dataclass
class ThinkPart:
    text: str
    TYPE: str = "think"


@dataclass
class ToolCallPart:
    id: str
    name: str
    arguments: str = ""
    TYPE: str = "tool_call"


@dataclass
class ToolResultPart:
    tool_call_id: str
    output: str = ""
    error: str = ""
    TYPE: str = "tool_result"


# ---------------------------------------------------------------------------
# Turn / step lifecycle events
# ---------------------------------------------------------------------------


@dataclass
class TurnBegin:
    user_input: Any = ""


@dataclass
class SteerInput:
    user_input: Any = ""


@dataclass
class TurnEnd:
    pass


@dataclass
class StepBegin:
    n: int = 0


@dataclass
class StepInterrupted:
    pass


@dataclass
class StepRetry:
    n: int = 0
    next_attempt: int = 1
    max_attempts: int = 1
    wait_s: float = 0.0
    error_type: str = ""
    status_code: int | None = None


@dataclass
class CompactionBegin:
    pass


@dataclass
class CompactionEnd:
    pass


@dataclass
class HookTriggered:
    event: str = ""
    target: str = ""
    hook_count: int = 1


@dataclass
class HookResolved:
    event: str = ""
    target: str = ""
    action: Literal["allow", "block"] = "allow"
    reason: str = ""
    duration_ms: int = 0


@dataclass
class MCPLoadingBegin:
    pass


@dataclass
class MCPLoadingEnd:
    pass


@dataclass
class MCPServerSnapshot:
    name: str = ""
    status: Literal["pending", "connecting", "connected", "failed", "unauthorized"] = "pending"
    tools: tuple[str, ...] = ()


@dataclass
class MCPStatusSnapshot:
    loading: bool = False
    connected: int = 0
    total: int = 0
    tools: int = 0
    servers: tuple[MCPServerSnapshot, ...] = ()


@dataclass
class StatusUpdate:
    context_usage: float | None = None
    context_tokens: int | None = None
    max_context_tokens: int | None = None
    token_usage: dict[str, Any] | None = None
    message_id: str | None = None
    plan_mode: bool | None = None
    mcp_status: MCPStatusSnapshot | None = None


@dataclass
class Notification:
    id: str = ""
    category: str = ""
    type: str = ""
    source_kind: str = ""
    source_id: str = ""
    title: str = ""
    body: str = ""
    severity: str = "info"
    created_at: float = 0.0
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlanDisplay:
    content: str = ""
    file_path: str = ""


@dataclass
class BtwBegin:
    id: str = ""
    question: str = ""


@dataclass
class BtwEnd:
    id: str = ""
    response: str | None = None
    error: str | None = None


@dataclass
class SubagentEvent:
    event: Any = None
    parent_tool_call_id: str | None = None
    agent_id: str | None = None
    subagent_type: str | None = None


ApprovalKind = Literal["approve", "approve_for_session", "reject"]


@dataclass
class ApprovalResponse:
    request_id: str = ""
    response: ApprovalKind = "approve"
    feedback: str = ""


@dataclass
class ApprovalRequest:
    id: str = ""
    tool_call_id: str = ""
    sender: str = ""
    action: str = ""
    description: str = ""
    source_kind: str | None = None
    source_id: str | None = None
    agent_id: str | None = None
    subagent_type: str | None = None
    source_description: str | None = None
    display: list[Any] = field(default_factory=list)
    _future: Any = field(default=None, repr=False, compare=False)
    _feedback: str = field(default="", repr=False, compare=False)

    def _get_future(self) -> asyncio.Future:
        if self._future is None:
            self._future = asyncio.get_event_loop().create_future()
        return self._future

    async def wait(self) -> ApprovalKind:
        return await self._get_future()

    def resolve(self, response: ApprovalKind, feedback: str = "") -> None:
        self._feedback = feedback
        fut = self._get_future()
        if not fut.done():
            fut.set_result(response)

    @property
    def feedback(self) -> str:
        return self._feedback

    @property
    def resolved(self) -> bool:
        return self._future is not None and self._future.done()


@dataclass
class QuestionOption:
    label: str = ""
    description: str = ""


@dataclass
class QuestionItem:
    question: str = ""
    options: list[QuestionOption] = field(default_factory=list)
    header: str = ""
    multi_select: bool = False
    body: str = ""
    other_label: str = ""
    other_description: str = ""


@dataclass
class QuestionResponse:
    request_id: str = ""
    answers: dict[str, str] = field(default_factory=dict)


class QuestionNotSupported(Exception):
    pass


@dataclass
class QuestionRequest:
    id: str = ""
    tool_call_id: str = ""
    questions: list[QuestionItem] = field(default_factory=list)
    _future: Any = field(default=None, repr=False, compare=False)

    def _get_future(self) -> asyncio.Future:
        if self._future is None:
            self._future = asyncio.get_event_loop().create_future()
        return self._future

    async def wait(self) -> dict[str, str]:
        return await self._get_future()

    def resolve(self, answers: dict[str, str]) -> None:
        fut = self._get_future()
        if not fut.done():
            fut.set_result(answers)

    def set_exception(self, exc: BaseException) -> None:
        fut = self._get_future()
        if not fut.done():
            fut.set_exception(exc)

    @property
    def resolved(self) -> bool:
        return self._future is not None and self._future.done()


@dataclass
class ToolCallRequest:
    id: str = ""
    name: str = ""
    arguments: str | None = None
    _future: Any = field(default=None, repr=False, compare=False)

    @staticmethod
    def from_tool_call(tool_call: Any) -> ToolCallRequest:
        if isinstance(tool_call, dict):
            fn = tool_call.get("function", {})
            return ToolCallRequest(
                id=str(tool_call.get("id", "")),
                name=str(fn.get("name", "")),
                arguments=fn.get("arguments"),
            )
        fn = getattr(tool_call, "function", None)
        return ToolCallRequest(
            id=str(getattr(tool_call, "id", "")),
            name=str(getattr(fn, "name", "") if fn else ""),
            arguments=getattr(fn, "arguments", None) if fn else None,
        )

    def _get_future(self) -> asyncio.Future:
        if self._future is None:
            self._future = asyncio.get_event_loop().create_future()
        return self._future

    async def wait(self) -> Any:
        return await self._get_future()

    def resolve(self, result: Any) -> None:
        fut = self._get_future()
        if not fut.done():
            fut.set_result(result)

    @property
    def resolved(self) -> bool:
        return self._future is not None and self._future.done()


@dataclass
class HookResponse:
    request_id: str = ""
    action: Literal["allow", "block"] = "allow"
    reason: str = ""


@dataclass
class HookRequest:
    id: str = ""
    subscription_id: str = ""
    event: str = ""
    target: str = ""
    input_data: dict[str, Any] = field(default_factory=dict)
    _future: Any = field(default=None, repr=False, compare=False)

    def _get_future(self) -> asyncio.Future:
        if self._future is None:
            self._future = asyncio.get_event_loop().create_future()
        return self._future

    async def wait(self) -> tuple[str, str]:
        return await self._get_future()

    def resolve(self, action: str, reason: str = "") -> None:
        fut = self._get_future()
        if not fut.done():
            fut.set_result((action, reason))

    @property
    def resolved(self) -> bool:
        return self._future is not None and self._future.done()


Event = Union[
    TurnBegin,
    SteerInput,
    TurnEnd,
    StepBegin,
    StepInterrupted,
    StepRetry,
    HookTriggered,
    HookResolved,
    CompactionBegin,
    CompactionEnd,
    MCPLoadingBegin,
    MCPLoadingEnd,
    StatusUpdate,
    Notification,
    TextPart,
    ThinkPart,
    ToolCallPart,
    ToolResultPart,
    ApprovalResponse,
    SubagentEvent,
    PlanDisplay,
    BtwBegin,
    BtwEnd,
]

Request = Union[ApprovalRequest, ToolCallRequest, QuestionRequest, HookRequest]

WireMessage = Union[Event, Request]

try:
    from kosong.message import (
        AudioURLPart as KosongAudioURLPart,
        ImageURLPart as KosongImageURLPart,
        TextPart as KosongTextPart,
        ThinkPart as KosongThinkPart,
        ToolCall as KosongToolCall,
        ToolCallPart as KosongToolCallPart,
        VideoURLPart as KosongVideoURLPart,
    )
    from kosong.tooling import ToolResult as KosongToolResult
except ImportError:
    KosongTextPart = None  # type: ignore
    KosongThinkPart = None  # type: ignore
    KosongToolCallPart = None  # type: ignore
    KosongToolCall = None  # type: ignore
    KosongImageURLPart = None  # type: ignore
    KosongAudioURLPart = None  # type: ignore
    KosongVideoURLPart = None  # type: ignore
    KosongToolResult = None  # type: ignore

EVENT_TYPES: tuple[type, ...] = tuple(
    cls
    for cls in (
        TurnBegin,
        SteerInput,
        TurnEnd,
        StepBegin,
        StepInterrupted,
        StepRetry,
        HookTriggered,
        HookResolved,
        CompactionBegin,
        CompactionEnd,
        MCPLoadingBegin,
        MCPLoadingEnd,
        StatusUpdate,
        Notification,
        TextPart,
        ThinkPart,
        ToolCallPart,
        ToolResultPart,
        ApprovalResponse,
        SubagentEvent,
        PlanDisplay,
        BtwBegin,
        BtwEnd,
        KosongToolResult,
        KosongTextPart,
        KosongThinkPart,
        KosongToolCallPart,
        KosongToolCall,
        KosongImageURLPart,
        KosongAudioURLPart,
        KosongVideoURLPart,
    )
    if cls is not None
)

REQUEST_TYPES: tuple[type, ...] = (
    ApprovalRequest,
    ToolCallRequest,
    QuestionRequest,
    HookRequest,
)


def is_event(msg: Any) -> bool:
    return isinstance(msg, EVENT_TYPES)


def is_request(msg: Any) -> bool:
    return isinstance(msg, REQUEST_TYPES)


def is_wire_message(msg: Any) -> bool:
    return is_event(msg) or is_request(msg)


_NAME_TO_TYPE: dict[str, type] = {cls.__name__: cls for cls in (*EVENT_TYPES, *REQUEST_TYPES)}
# Backwards-compat alias.
_NAME_TO_TYPE["ApprovalRequestResolved"] = ApprovalResponse
if KosongToolResult is not None:
    _NAME_TO_TYPE["ToolResult"] = KosongToolResult


@dataclass
class WireMessageEnvelope:
    type: str
    payload: dict[str, Any]

    @classmethod
    def from_wire_message(cls, msg: Any) -> WireMessageEnvelope:
        typename: str | None = None
        for name, typ in _NAME_TO_TYPE.items():
            if isinstance(msg, typ):
                typename = name
                break
        if typename is None:
            raise ValueError(f"Unknown wire message type: {type(msg)}")
        return cls(type=typename, payload=_to_json_dict(msg))

    def to_wire_message(self) -> Any:
        msg_type = _NAME_TO_TYPE.get(self.type)
        if msg_type is None:
            raise ValueError(f"Unknown wire message type: {self.type}")
        return _from_json_dict(msg_type, self.payload)


def _to_json_dict(msg: Any) -> dict[str, Any]:
    if hasattr(msg, "model_dump"):
        return _jsonable(msg.model_dump())
    if dataclasses.is_dataclass(msg):
        out: dict[str, Any] = {}
        for f in dataclasses.fields(msg):
            if f.name.startswith("_"):
                continue
            out[f.name] = _jsonable(getattr(msg, f.name))
        return out
    if isinstance(msg, dict):
        return {str(k): _jsonable(v) for k, v in msg.items()}
    return {"value": _jsonable(msg)}


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, (pathlib.Path, pathlib.PurePath)):
        return str(v)
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", errors="replace")
    if hasattr(v, "model_dump"):
        return _jsonable(v.model_dump())
    if hasattr(v, "dict") and callable(getattr(v, "dict", None)):
        return _jsonable(v.dict())
    if isinstance(v, (list, tuple, set, frozenset)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if dataclasses.is_dataclass(v):
        return _to_json_dict(v)
    return str(v)


def _from_json_dict(cls: type, payload: dict[str, Any]) -> Any:
    if not dataclasses.is_dataclass(cls):
        return payload
    kwargs: dict[str, Any] = {}
    try:
        fields = {f.name: f for f in dataclasses.fields(cls)}
    except Exception:
        return payload
    for name, f in fields.items():
        if name.startswith("_"):
            continue
        if name in payload:
            kwargs[name] = _coerce(payload[name], f.type)
    try:
        return cls(**kwargs)
    except Exception:
        # Best effort: fill defaults for missing fields.
        return cls()  # type: ignore[call-arg]


def _coerce(value: Any, _type: Any) -> Any:
    return value


def serialize_wire_message(msg: Any) -> dict[str, Any]:
    envelope = WireMessageEnvelope.from_wire_message(msg)
    return {"type": envelope.type, "payload": envelope.payload}


def deserialize_wire_message(data: Any) -> Any:
    if not isinstance(data, dict):
        raise ValueError("wire message must be a dict")
    envelope = WireMessageEnvelope(
        type=str(data.get("type", "")), payload=dict(data.get("payload", {}))
    )
    return envelope.to_wire_message()
