"""LLM error normalization and credential-safe diagnostics — port of deepcode core/src/common/llm-error.ts."""

from __future__ import annotations

import re
from typing import Any

MAX_MESSAGE_LENGTH = 500
MAX_CAUSE_DEPTH = 5

SENSITIVE_PATTERNS = [
    (re.compile(r"(Authorization\s*[:=]\s*(?:Bearer\s+)?)[^\s,;]+", re.IGNORECASE), r"\1***MASKED***"),
    (re.compile(r"([?&](?:api[_-]?key|access[_-]?token|token)=)[^&\s]+", re.IGNORECASE), r"\1***MASKED***"),
    (
        re.compile(
            r"""(["']?(?:api[_-]?key|access[_-]?token|secret)["']?\s*[:=]\s*["']?)[^",}&;\s]+""",
            re.IGNORECASE,
        ),
        r"\1***MASKED***",
    ),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"), "***MASKED***"),
]



def mask_sensitive(text: str) -> str:
    """Mask API keys, authorization bearer tokens, and secret parameters from text."""
    if not text:
        return ""
    result = text
    for pattern, replacement in SENSITIVE_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def _safe_text(value: Any, max_length: int = MAX_MESSAGE_LENGTH) -> str | None:
    if value is None:
        return None
    if not isinstance(value, (str, int, float)):
        return None
    normalized = " ".join(mask_sensitive(str(value)).split()).strip()
    if not normalized:
        return None
    return f"{normalized[:max_length]}..." if len(normalized) > max_length else normalized


def _get_header(headers: Any, name: str) -> str | None:
    if not headers:
        return None
    target = name.lower()
    if hasattr(headers, "get") and callable(headers.get):
        val = headers.get(name) or headers.get(target)
        if val is not None:
            return _safe_text(val)
    if isinstance(headers, dict):
        for k, v in headers.items():
            if str(k).lower() == target:
                return _safe_text(v)
    return None


def _is_generic_connection_message(message: str) -> bool:
    normalized = " ".join((message or "").lower().split()).strip()
    return normalized in {
        "connection error",
        "connection error.",
        "fetch failed",
        "request timed out.",
        "request timed out",
    }


def _find_useful_cause_message(causes: list[dict[str, Any]]) -> str | None:
    for cause in causes:
        msg = cause.get("message", "")
        if not _is_generic_connection_message(msg):
            return msg
        nested = _find_useful_cause_message(cause.get("causes", []))
        if nested:
            return nested
    return None


def _format_error_parts(parts: list[str]) -> str:
    if not parts:
        return "Unknown error"
    first = parts[0]
    metadata = parts[1:]
    return f"{first} [{', '.join(metadata)}]" if metadata else first


def _get_provider_message(error: Any) -> str | None:
    if isinstance(error, dict):
        err_obj = error.get("error")
        if isinstance(err_obj, dict):
            return _safe_text(err_obj.get("message"))
    if hasattr(error, "body") and isinstance(getattr(error, "body"), dict):
        err_obj = getattr(error, "body").get("error")
        if isinstance(err_obj, dict):
            return _safe_text(err_obj.get("message"))
    return None


def _get_error_details_recursive(error: Any, depth: int, seen: set[int]) -> dict[str, Any]:
    obj_id = id(error)
    if obj_id in seen or depth >= MAX_CAUSE_DEPTH:
        return {"name": "Error", "message": "Recursive error"}
    seen.add(obj_id)

    name = "UnknownError"
    message = "Unknown error"
    status = None
    code = None
    err_type = None
    param = None
    request_id = None
    trace_id = None
    stack = None
    causes: list[dict[str, Any]] = []

    if isinstance(error, dict):
        name = _safe_text(error.get("name")) or "Error"
        message = _safe_text(error.get("message")) or "Unknown error"
        status = error.get("status") if isinstance(error.get("status"), int) else None
        code = _safe_text(error.get("code"))
        err_type = _safe_text(error.get("type"))
        param = _safe_text(error.get("param"))
        request_id = _safe_text(error.get("requestID") or error.get("requestId")) or _get_header(
            error.get("headers"), "x-request-id"
        )
        trace_id = _get_header(error.get("headers"), "x-ds-trace-id")
        stack = _safe_text(error.get("stack"), MAX_MESSAGE_LENGTH * 4)

        if "cause" in error and error["cause"] is not None:
            causes.append(_get_error_details_recursive(error["cause"], depth + 1, seen))
    elif isinstance(error, Exception):
        name = type(error).__name__
        message = _safe_text(str(error)) or name

        status_val = (
            getattr(error, "status_code", None)
            or getattr(error, "status", None)
            or getattr(error, "http_status", None)
        )
        if isinstance(status_val, int):
            status = status_val

        code = _safe_text(getattr(error, "code", None))
        err_type = _safe_text(getattr(error, "type", None))
        param = _safe_text(getattr(error, "param", None))

        response = getattr(error, "response", None)
        headers = getattr(response, "headers", None) or getattr(error, "headers", None)
        request_id = (
            _safe_text(getattr(error, "request_id", None))
            or _safe_text(getattr(error, "requestID", None))
            or _get_header(headers, "x-request-id")
        )
        trace_id = _get_header(headers, "x-ds-trace-id")

        cause = getattr(error, "__cause__", None) or getattr(error, "__context__", None)
        if cause is not None and isinstance(cause, Exception):
            causes.append(_get_error_details_recursive(cause, depth + 1, seen))
    else:
        message = _safe_text(str(error)) or "Unknown error"

    details: dict[str, Any] = {"name": name, "message": message}
    if status is not None:
        details["status"] = status
    if code:
        details["code"] = code
    if err_type:
        details["type"] = err_type
    if param:
        details["param"] = param
    if request_id:
        details["requestId"] = request_id
    if trace_id:
        details["traceId"] = trace_id
    if stack:
        details["stack"] = stack
    if causes:
        details["causes"] = causes

    return details


def get_llm_error_details(error: Any) -> dict[str, Any]:
    """Extract serializable diagnostics from an error object."""
    return _get_error_details_recursive(error, 0, set())


def describe_llm_error(error: Any) -> str:
    """Produce a concise, credential-safe explanation from API errors and underlying network causes."""
    details = get_llm_error_details(error)
    parts: list[str] = []

    if details.get("status") is not None:
        provider_msg = _get_provider_message(error)
        msg = provider_msg or details.get("message") or "Error"
        parts.append(f"HTTP {details['status']}: {msg}")
        if details.get("code"):
            parts.append(f"code: {details['code']}")
        if details.get("type"):
            parts.append(f"type: {details['type']}")
        if details.get("param"):
            parts.append(f"param: {details['param']}")
        if details.get("requestId"):
            parts.append(f"request ID: {details['requestId']}")
        if details.get("traceId"):
            parts.append(f"trace ID: {details['traceId']}")
        return _format_error_parts(parts)

    causes = details.get("causes", [])
    cause_message = _find_useful_cause_message(causes)
    main_message = details.get("message") or "Unknown error"

    if cause_message and _is_generic_connection_message(main_message):
        return f"Connection error: {cause_message}"
    if cause_message and cause_message != main_message:
        return f"{main_message} (cause: {cause_message})"
    return main_message
