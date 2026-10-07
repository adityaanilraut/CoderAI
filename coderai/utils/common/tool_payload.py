"""Compact model-facing tool results without corrupting structured payloads."""

from __future__ import annotations

import json
from typing import Any


def project_tool_metadata(name: str, metadata: dict[str, Any], has_output: bool) -> dict[str, Any]:
    """Keep actionable metadata; UI-only copies of displayed results stay out of prompts."""
    redundant = {"grep": {"matches"}, "glob": {"paths"}}.get(name.lower(), set())
    return {k: v for k, v in metadata.items() if not (has_output and k in redundant)}


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 0:
        return ""
    marker = f"\n...[{len(text) - limit} characters omitted]...\n"
    if len(marker) > limit:
        return text[:limit]
    room = max(0, limit - len(marker))
    head = (room + 1) // 2
    tail = room // 2
    return text[:head] + marker + (text[-tail:] if tail else "")


def prune_tool_payload(content: str, max_chars: int) -> str:
    """Preserve JSON envelopes and exit metadata while bounding model context."""
    if max_chars < 2:
        raise ValueError("Structured tool output requires a limit of at least two characters")
    if not content or len(content) <= max_chars:
        return content
    try:
        payload = json.loads(content)
    except (ValueError, TypeError):
        return _shorten(content, max_chars)
    if not isinstance(payload, dict):
        return _bounded_fallback({}, content, max_chars)
    # Large diagnostic objects are not useful twice alongside formatted output.
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        metadata = project_tool_metadata(
            str(payload.get("name", "")), metadata, "output" in payload
        )
        omitted = [k for k, v in metadata.items() if len(json.dumps(v)) > max_chars // 4]
        metadata = {k: v for k, v in metadata.items() if k not in omitted}
        if omitted:
            metadata["omitted_fields"] = omitted
        payload["metadata"] = metadata
    payload["truncated"] = True
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= max_chars:
        return encoded
    # Account for JSON escaping, instead of cutting serialized JSON in the middle.
    strings = [k for k, v in payload.items() if isinstance(v, str) and k not in {"name"}]
    for key in sorted(strings, key=lambda k: len(payload[k]), reverse=True):
        original = payload[key]
        lo, hi = 0, len(original)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            payload[key] = _shorten(original, mid)
            if len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))) <= max_chars:
                lo = mid
            else:
                hi = mid - 1
        payload[key] = _shorten(original, lo)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) <= max_chars:
            return encoded
    # Unknown nested payloads keep essential status while dropping bulky structure.
    return _bounded_fallback(payload, encoded, max_chars)


def _bounded_fallback(payload: dict[str, Any], output: str, limit: int) -> str:
    """Fit escaped output into a compact, parseable envelope, even for tiny limits."""

    def encode(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    minimal: dict[str, Any] = {"truncated": True}
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        essential = {k: v for k, v in metadata.items() if k in {"exitCode", "exit", "snippet_id"}}
        if essential and len(encode({**minimal, "metadata": essential})) <= limit:
            minimal["metadata"] = essential
    for key in ("ok", "name"):
        if key in payload and len(encode({**minimal, key: payload[key]})) <= limit:
            minimal[key] = payload[key]
    if len(encode(minimal)) > limit:
        return "{}"
    if len(encode({**minimal, "output": ""})) > limit:
        return encode(minimal)
    lo, hi = 0, len(output)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(encode({**minimal, "output": _shorten(output, mid)})) <= limit:
            lo = mid
        else:
            hi = mid - 1
    return encode({**minimal, "output": _shorten(output, lo)})
