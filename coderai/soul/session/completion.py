"""Completion streaming, sync execution, and tool call serialization helpers."""

from __future__ import annotations

import json
import pathlib
import uuid
from collections.abc import Callable
from typing import Any

from coderai.utils.common.openai_thinking import (
    extract_reasoning_content,
    reasoning_key_for_model,
)
from coderai.utils.common.usage import extract_usage_dict
from coderai.soul.approval import resolve_snippet_file_path
from coderai.soul.session.streaming import assemble_stream_response


def normalize_tool_calls(raw: Any) -> list[dict[str, Any]] | None:
    """Normalize model calls with unique identities before permission planning."""
    if not raw:
        return None
    result: list[dict[str, Any]] = []
    for tc in raw:
        if isinstance(tc, dict):
            tc_id = tc.get("id")
            func = tc.get("function") or {}
            result.append(
                {
                    "id": tc_id,
                    "type": "function",
                    "function": {
                        "name": func.get("name", ""),
                        "arguments": func.get("arguments", ""),
                    },
                }
            )
        else:
            tc_id = getattr(tc, "id", None)
            result.append(
                {
                    "id": tc_id,
                    "type": "function",
                    "function": {
                        "name": getattr(getattr(tc, "function", None), "name", "") or "",
                        "arguments": getattr(getattr(tc, "function", None), "arguments", "") or "",
                    },
                }
            )
    # Reserve original IDs before generating replacements, including IDs on
    # later calls, so one approval can never address two model invocations.
    reserved_ids = {call["id"] for call in result if isinstance(call["id"], str) and call["id"]}
    seen_ids: set[str] = set()
    for call in result:
        call_id = call["id"]
        if not isinstance(call_id, str) or not call_id or call_id in seen_ids:
            call_id = uuid.uuid4().hex
            while call_id in reserved_ids or call_id in seen_ids:
                call_id = uuid.uuid4().hex
            call["id"] = call_id
        seen_ids.add(call_id)
    return result or None


def pydantic_tool_calls(message: Any) -> list[dict[str, Any]] | None:
    """Extract tool calls from a Pydantic message object."""
    raw = getattr(message, "tool_calls", None)
    if not raw:
        return None
    out: list[dict[str, Any]] = []
    for tc in raw:
        func = getattr(tc, "function", None)
        out.append(
            {
                "id": getattr(tc, "id", "") or uuid.uuid4().hex,
                "type": "function",
                "function": {
                    "name": getattr(func, "name", "") or "",
                    "arguments": getattr(func, "arguments", "") or "",
                },
            }
        )
    return out or None


def format_completion_response(resp: Any, reasoning_key: str | None = None) -> dict[str, Any]:
    """Format an OpenAI SDK or dict completion response into a standardized structure."""
    if isinstance(resp, dict):
        return resp

    message = getattr(resp.choices[0], "message", None)
    if isinstance(message, dict):
        details = message.get("reasoning_details")
    else:
        details = getattr(message, "reasoning_details", None)
    result: dict[str, Any] = {
        "choices": [
            {
                "message": {
                    "content": getattr(message, "content", None) or "",
                    "tool_calls": pydantic_tool_calls(message),
                    "reasoning_content": extract_reasoning_content(message, reasoning_key),
                    "reasoning_details": details,
                    "refusal": getattr(message, "refusal", None),
                }
            }
        ]
    }
    usage = getattr(resp, "usage", None)
    if usage is not None:
        result["usage"] = extract_usage_dict(usage)
    return result


def _create_with_reasoning_retry(client: Any, request: dict[str, Any]) -> Any:
    """Recover once when stale/missing OpenRouter metadata disabled mandatory reasoning."""
    try:
        return client.chat.completions.create(**request)
    except Exception as error:
        from coderai.openrouter import is_openrouter_model

        extra_body = request.get("extra_body")
        if not isinstance(extra_body, dict):
            raise
        reasoning = extra_body.get("reasoning")
        if not isinstance(reasoning, dict):
            raise
        disabled = reasoning.get("enabled") is False or reasoning.get("effort") == "none"
        if not (
            getattr(error, "status_code", None) == 400
            and "reasoning is mandatory" in str(error).lower()
            and disabled
            and is_openrouter_model(
                str(request.get("model") or ""), str(getattr(client, "base_url", "") or "")
            )
        ):
            raise
        retry_reasoning = {**reasoning, "enabled": True, "exclude": True}
        if retry_reasoning.get("effort") == "none":
            retry_reasoning.pop("effort")
        retry_request = {**request, "extra_body": {**extra_body, "reasoning": retry_reasoning}}
        return client.chat.completions.create(**retry_request)


def call_sync(client: Any, request: dict[str, Any]) -> dict[str, Any]:
    """Execute a synchronous completion request."""
    resp = _create_with_reasoning_retry(client, request)
    return format_completion_response(
        resp, reasoning_key_for_model(str(request.get("model") or ""))
    )


def call_stream_or_sync(
    client: Any,
    request: dict[str, Any],
    on_chunk: Callable[[str], None] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    on_thinking_chunk: Callable[[str], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Stream response tokens while parsing deltas, tool calls, and reasoning tokens."""
    stream_req = {
        **request,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    reasoning_key = reasoning_key_for_model(str(request.get("model") or ""))
    try:
        try:
            if is_cancelled and is_cancelled():
                from coderai.soul.session.manager import SessionInterrupted

                raise SessionInterrupted("Completion cancelled")
            resp = _create_with_reasoning_retry(client, stream_req)
        except Exception as err:
            if is_cancelled and is_cancelled():
                from coderai.soul.session.manager import SessionInterrupted

                raise SessionInterrupted("Completion cancelled") from err
            err_msg = str(err).lower()
            if "reasoning_effort" in err_msg or "stream_options" in err_msg:
                retry_req = dict(stream_req)
                if "stream_options" in err_msg:
                    retry_req.pop("stream_options", None)
                if "reasoning_effort" in err_msg:
                    if "none" in err_msg:
                        retry_req["reasoning_effort"] = "none"
                    else:
                        retry_req.pop("reasoning_effort", None)
                        if isinstance(retry_req.get("extra_body"), dict):
                            retry_req["extra_body"].pop("reasoning_effort", None)
                            if not retry_req["extra_body"]:
                                retry_req.pop("extra_body", None)
                resp = _create_with_reasoning_retry(client, retry_req)
            elif "reasoning_details" in err_msg:
                retry_req = dict(stream_req)
                retry_req["messages"] = [
                    {k: v for k, v in msg.items() if k != "reasoning_details"}
                    if isinstance(msg, dict)
                    else msg
                    for msg in retry_req.get("messages") or []
                ]
                resp = _create_with_reasoning_retry(client, retry_req)
            else:
                raise

        if isinstance(resp, dict):
            return resp

        if hasattr(resp, "choices"):
            return format_completion_response(resp, reasoning_key)

        if hasattr(resp, "__iter__"):
            return assemble_stream_response(
                resp,
                reasoning_key,
                on_chunk=on_chunk,
                on_progress=on_progress,
                on_thinking_chunk=on_thinking_chunk,
                is_cancelled=is_cancelled,
            )
    except (TypeError, AttributeError):
        return call_sync(client, request)
    return call_sync(client, request)


def resolve_target_file_path(session_id: str, project_root: str, tc: Any) -> str | None:
    """Extract and resolve target file path from tool call arguments."""
    if isinstance(tc, dict):
        args_raw = tc.get("function", {}).get("arguments", "{}")
    else:
        func = getattr(tc, "function", None)
        args_raw = getattr(func, "arguments", "{}") if func else "{}"
    try:
        args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
    except Exception:
        args = {}
    if not isinstance(args, dict):
        return None
    fp = args.get("file_path")
    if isinstance(fp, str) and fp.strip():
        p = pathlib.Path(fp)
        return (
            str(p.resolve()) if p.is_absolute() else str((pathlib.Path(project_root) / p).resolve())
        )
    snippet_id = args.get("snippet_id")
    if isinstance(snippet_id, str) and snippet_id.strip():
        return resolve_snippet_file_path(session_id, snippet_id)
    return None


def is_invisible_execution(content: str) -> bool:
    """Determine whether tool execution result was marked invisible to the UI."""
    try:
        data = json.loads(content)
        return bool(data.get("metadata", {}).get("invisible") is True)
    except Exception:
        return False


def build_tool_params_snippet(tool_function: Any) -> str:
    """Generate a brief one-line snippet of tool parameters for UI display."""
    if not tool_function:
        return ""
    if isinstance(tool_function, dict):
        name = tool_function.get("name", "")
        args = tool_function.get("arguments", "{}")
    else:
        name = getattr(tool_function, "name", "")
        args = getattr(tool_function, "arguments", "{}")
    return f"`{name}`: {str(args)[:100]}"


def build_tool_result_snippet(content: str) -> str:
    """Generate a brief one-line snippet of tool result for UI display."""
    try:
        data = json.loads(content)
        if data.get("ok"):
            out = str(data.get("output") or "OK")
            return out[:100] + ("..." if len(out) > 100 else "")
        err = str(data.get("error") or "Error")
        return f"Error: {err[:100]}"
    except Exception:
        return content[:100]
