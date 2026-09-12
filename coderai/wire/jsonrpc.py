"""JSON-RPC 2.0 framing for the wire server.

Inbound (client → server): initialize / prompt / steer / replay /
set_plan_mode / cancel (+ success/error responses to our requests).
Outbound (server → client): event / request (+ success/error responses).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


class ErrorCodes:
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    INVALID_STATE = -32000
    LLM_NOT_SET = -32001
    LLM_NOT_SUPPORTED = -32002
    CHAT_PROVIDER_ERROR = -32003
    AUTH_EXPIRED = -32004


class Statuses:
    FINISHED = "finished"
    CANCELLED = "cancelled"
    MAX_STEPS_REACHED = "max_steps_reached"
    STEERED = "steered"


@dataclass
class JSONRPCErrorObject:
    code: int = ErrorCodes.INTERNAL_ERROR
    message: str = ""
    data: Any = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            d["data"] = self.data
        return d


@dataclass
class JSONRPCMessage:
    method: str | None = None
    id: Any = None
    params: Any = None
    result: Any = None
    error: JSONRPCErrorObject | dict[str, Any] | None = None
    jsonrpc: Literal["2.0"] = "2.0"

    def method_is_inbound(self) -> bool:
        return self.method in JSONRPC_IN_METHODS

    def is_request(self) -> bool:
        return self.method is not None and self.id is not None

    def is_notification(self) -> bool:
        return self.method is not None and self.id is None

    def is_response(self) -> bool:
        return self.method is None and self.id is not None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"jsonrpc": self.jsonrpc}
        if self.method is not None:
            d["method"] = self.method
        if self.id is not None:
            d["id"] = self.id
        if self.params is not None:
            d["params"] = self.params
        if self.result is not None:
            d["result"] = self.result
        if self.error is not None:
            d["error"] = (
                self.error.to_dict() if isinstance(self.error, JSONRPCErrorObject) else self.error
            )
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JSONRPCMessage:
        err = data.get("error")
        err_obj = None
        if isinstance(err, dict):
            err_obj = JSONRPCErrorObject(
                code=int(err.get("code", ErrorCodes.INTERNAL_ERROR)),
                message=str(err.get("message", "")),
                data=err.get("data"),
            )
        return cls(
            method=data.get("method"),
            id=data.get("id"),
            params=data.get("params"),
            result=data.get("result"),
            error=err_obj,
        )


def success_response(id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id, "result": result}


def error_response(id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": id, "error": err}


def event_notification(payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": "event", "params": payload}


def request_message(id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": "request", "id": id, "params": payload}


@dataclass
class ClientInfo:
    name: str = ""
    version: str | None = None


@dataclass
class ExternalTool:
    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass
class ClientCapabilities:
    supports_question: bool = False
    supports_plan_mode: bool = False


@dataclass
class WireHookSubscription:
    id: str = ""
    event: str = ""
    matcher: str = ""
    timeout: int = 30


JSONRPC_IN_METHODS = {"initialize", "prompt", "steer", "replay", "set_plan_mode", "cancel"}
JSONRPC_OUT_METHODS = {"event", "request"}
