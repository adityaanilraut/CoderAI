"""LLM error normalization — port of deepcode core/src/common/llm-error.ts."""

from __future__ import annotations

from typing import Any

MAX_MESSAGE_LENGTH = 500


def get_llm_error_details(error: Any) -> dict:
    if isinstance(error, dict):
        return {
            "name": error.get("name", "Error"),
            "message": str(error.get("message", ""))[:MAX_MESSAGE_LENGTH],
            "status": error.get("status"),
            "code": error.get("code"),
        }
    if isinstance(error, Exception):
        status = getattr(error, "status_code", None) or getattr(error, "status", None)
        return {
            "name": type(error).__name__,
            "message": str(error)[:MAX_MESSAGE_LENGTH],
            "status": status,
            "code": getattr(error, "code", None),
        }
    return {"name": "Error", "message": str(error)[:MAX_MESSAGE_LENGTH]}


def describe_llm_error(error: Any) -> str:
    details = get_llm_error_details(error)
    if details.get("status") is not None:
        text = f"HTTP {details['status']}: {details['message']}"
        if details.get("code"):
            text += f" ({details['code']})"
        return text
    return f"{details['name']}: {details['message']}"
