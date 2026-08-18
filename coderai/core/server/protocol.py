"""JSON-RPC 2.0 protocol specifications and serialization helpers for CoderAI server."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
SERVER_NOT_INITIALIZED = -32002


@dataclass
class JsonRpcRequest:
    id: Any
    method: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class JsonRpcResponse:
    id: Any
    result: Any = None
    error: dict[str, Any] | None = None


@dataclass
class JsonRpcNotification:
    method: str
    params: dict[str, Any] = field(default_factory=dict)


def format_response(req_id: Any, result: Any) -> dict[str, Any]:
    """Format a successful JSON-RPC 2.0 response dictionary."""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": result,
    }


def format_error(req_id: Any, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
    """Format a JSON-RPC 2.0 error response dictionary."""
    err: dict[str, Any] = {
        "code": code,
        "message": message,
    }
    if data is not None:
        err["data"] = data
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": err,
    }


def format_notification(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Format a JSON-RPC 2.0 notification event dictionary."""
    return {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
    }


def parse_message(
    raw_line: str,
) -> tuple[bool, dict[str, Any] | None, dict[str, Any] | None]:
    """Parse and validate an incoming raw JSON-RPC 2.0 line.

    Returns:
        tuple[bool, dict | None, dict | None]:
            (is_valid, parsed_payload, error_dict)
    """
    if not raw_line or not raw_line.strip():
        return False, None, None

    try:
        payload = json.loads(raw_line)
    except Exception as e:
        return False, None, format_error(None, PARSE_ERROR, f"Parse error: {e}")

    if not isinstance(payload, dict):
        return (
            False,
            None,
            format_error(None, INVALID_REQUEST, "Invalid Request: expected JSON object"),
        )

    if payload.get("jsonrpc") != "2.0":
        return (
            False,
            None,
            format_error(
                payload.get("id"),
                INVALID_REQUEST,
                "Invalid Request: missing or invalid jsonrpc version (must be '2.0')",
            ),
        )

    if "method" not in payload or not isinstance(payload["method"], str):
        return (
            False,
            None,
            format_error(
                payload.get("id"),
                INVALID_REQUEST,
                "Invalid Request: method string is required",
            ),
        )

    return True, payload, None
