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
from coderai.utils.common.validate import repair_json_string
from coderai.soul.approval import resolve_snippet_file_path


def normalize_tool_calls(raw: Any) -> list[dict[str, Any]] | None:
    """Convert heterogeneous tool call objects/dicts into a uniform list of dictionaries."""
    if not raw:
        return None
    result: list[dict[str, Any]] = []
    for tc in raw:
        if isinstance(tc, dict):
            tc_id = tc.get("id") or uuid.uuid4().hex
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
            tc_id = getattr(tc, "id", "") or uuid.uuid4().hex
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
    result: dict[str, Any] = {
        "choices": [
            {
                "message": {
                    "content": getattr(message, "content", None) or "",
                    "tool_calls": pydantic_tool_calls(message),
                    "reasoning_content": extract_reasoning_content(message, reasoning_key),
                    "refusal": getattr(message, "refusal", None),
                }
            }
        ]
    }
    usage = getattr(resp, "usage", None)
    if usage is not None:
        result["usage"] = extract_usage_dict(usage)
    return result


def call_sync(client: Any, request: dict[str, Any]) -> dict[str, Any]:
    """Execute a synchronous completion request."""
    resp = client.chat.completions.create(**request)
    return format_completion_response(
        resp, reasoning_key_for_model(str(request.get("model") or ""))
    )


def call_stream_or_sync(
    client: Any,
    request: dict[str, Any],
    on_chunk: Callable[[str], None] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    on_thinking_chunk: Callable[[str], None] | None = None,
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
            resp = client.chat.completions.create(**stream_req)
        except Exception as err:
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
                resp = client.chat.completions.create(**retry_req)
            else:
                raise

        if isinstance(resp, dict):
            return resp

        if hasattr(resp, "choices"):
            return format_completion_response(resp, reasoning_key)

        if hasattr(resp, "__iter__"):
            content_parts: list[str] = []
            thinking_parts: list[str] = []
            tool_calls_dict: dict[int, dict[str, Any]] = {}
            refusal_parts: list[str] = []
            usage_dict: dict[str, int] = {}
            estimated_tokens = 0

            try:
                for chunk in resp:
                    choices = getattr(chunk, "choices", None) or []
                    if choices:
                        c = choices[0]
                        delta = getattr(c, "delta", None)
                        if delta:
                            delta_content = getattr(delta, "content", None)
                            if delta_content:
                                content_parts.append(delta_content)
                                estimated_tokens += max(1, len(delta_content) // 4)
                                if on_chunk:
                                    try:
                                        on_chunk(delta_content)
                                    except Exception:
                                        pass
                                if on_progress:
                                    try:
                                        on_progress(
                                            {"estimatedTokens": estimated_tokens, "type": "update"}
                                        )
                                    except Exception:
                                        pass

                            delta_thinking = extract_reasoning_content(
                                delta, reasoning_key
                            ) or getattr(delta, "thinking", None)
                            if delta_thinking:
                                thinking_parts.append(delta_thinking)
                                estimated_tokens += max(1, len(delta_thinking) // 4)
                                if on_thinking_chunk:
                                    try:
                                        on_thinking_chunk(delta_thinking)
                                    except Exception:
                                        pass
                                if on_progress:
                                    try:
                                        on_progress(
                                            {
                                                "estimatedTokens": estimated_tokens,
                                                "type": "update",
                                                "isThinking": True,
                                            }
                                        )
                                    except Exception:
                                        pass

                            delta_refusal = getattr(delta, "refusal", None)
                            if delta_refusal:
                                refusal_parts.append(delta_refusal)

                            delta_tc = getattr(delta, "tool_calls", None)
                            if delta_tc:
                                for tc_delta in delta_tc:
                                    idx = getattr(tc_delta, "index", 0)
                                    if idx not in tool_calls_dict:
                                        tool_calls_dict[idx] = {
                                            "id": getattr(tc_delta, "id", "") or uuid.uuid4().hex,
                                            "type": "function",
                                            "function": {"name": "", "arguments": ""},
                                        }
                                    entry = tool_calls_dict[idx]
                                    if getattr(tc_delta, "id", None):
                                        entry["id"] = tc_delta.id
                                    func = getattr(tc_delta, "function", None)
                                    if func:
                                        if getattr(func, "name", None):
                                            entry["function"]["name"] += func.name
                                        if getattr(func, "arguments", None):
                                            entry["function"]["arguments"] += func.arguments

                    chunk_usage = getattr(chunk, "usage", None)
                    if chunk_usage:
                        usage_dict = extract_usage_dict(chunk_usage)
            except Exception as stream_err:
                if thinking_parts:
                    setattr(stream_err, "partial_thinking", "".join(thinking_parts))
                if content_parts:
                    setattr(stream_err, "partial_content", "".join(content_parts))
                raise stream_err

            if on_progress:
                try:
                    on_progress({"estimatedTokens": estimated_tokens, "type": "end"})
                except Exception:
                    pass

            if not usage_dict and estimated_tokens > 0:
                usage_dict = {
                    "prompt_tokens": 0,
                    "completion_tokens": estimated_tokens,
                    "total_tokens": estimated_tokens,
                    "cached_tokens": 0,
                    "prompt_cache_hit_tokens": 0,
                    "prompt_cache_miss_tokens": 0,
                }

            assembled_tool_calls: list[dict[str, Any]] = []
            for i in sorted(tool_calls_dict.keys()):
                tc = tool_calls_dict[i]
                raw_args = tc.get("function", {}).get("arguments", "")
                if raw_args:
                    tc["function"]["arguments"] = repair_json_string(raw_args)
                assembled_tool_calls.append(tc)

            tool_calls = assembled_tool_calls or None
            res: dict[str, Any] = {
                "choices": [
                    {
                        "message": {
                            "content": "".join(content_parts),
                            "tool_calls": tool_calls,
                            "reasoning_content": "".join(thinking_parts) or None,
                            "refusal": "".join(refusal_parts) or None,
                        }
                    }
                ]
            }
            if usage_dict:
                res["usage"] = usage_dict
            return res
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
