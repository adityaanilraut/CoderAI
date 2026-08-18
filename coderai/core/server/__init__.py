"""CoderAI JSON-RPC 2.0 Headless Server and IDE Companion Protocol."""

from coderai.core.server.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    SERVER_NOT_INITIALIZED,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    format_error,
    format_notification,
    format_response,
    parse_message,
)
from coderai.core.server.server import CoderAIServer

__all__ = [
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "PARSE_ERROR",
    "SERVER_NOT_INITIALIZED",
    "CoderAIServer",
    "JsonRpcNotification",
    "JsonRpcRequest",
    "JsonRpcResponse",
    "format_error",
    "format_notification",
    "format_response",
    "parse_message",
]
