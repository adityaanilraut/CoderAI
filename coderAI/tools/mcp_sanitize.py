"""Sanitization helpers extracted from mcp.py to keep the client focused."""

from __future__ import annotations

from typing import Any

MCP_MAX_DESCRIPTION_LENGTH = 1_024
MCP_MAX_METADATA_DEPTH = 12
MCP_MAX_METADATA_ITEMS = 1_000


def _sanitize_metadata_text(value: Any, limit: int = MCP_MAX_DESCRIPTION_LENGTH) -> str:
    """Collapse control/formatting characters and clamp untrusted model metadata."""
    if not isinstance(value, str):
        return ""
    cleaned = "".join(" " if ord(ch) < 32 or ord(ch) == 127 else ch for ch in value)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > limit:
        return cleaned[: limit - 3] + "..."
    return cleaned


def _sanitize_model_metadata(value: Any, *, depth: int = 0) -> Any:
    """Bound server-controlled structures returned to the model or used as schemas."""
    if depth >= MCP_MAX_METADATA_DEPTH:
        return None
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MCP_MAX_METADATA_ITEMS:
                break
            safe_key = _sanitize_metadata_text(str(key), 128)
            if not safe_key:
                continue
            if safe_key.lower() in {"description", "title", "$comment"}:
                out[safe_key] = _sanitize_metadata_text(item)
            else:
                out[safe_key] = _sanitize_model_metadata(item, depth=depth + 1)
        return out
    if isinstance(value, list):
        return [
            _sanitize_model_metadata(item, depth=depth + 1)
            for item in value[:MCP_MAX_METADATA_ITEMS]
        ]
    if isinstance(value, str):
        return _sanitize_metadata_text(value, 4_096)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return None
