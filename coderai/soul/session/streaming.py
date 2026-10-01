"""Provider stream assembly independent of client negotiation and sync fallback."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from typing import Any, Protocol

from coderai.utils.common.openai_thinking import extract_reasoning_content
from coderai.utils.common.usage import extract_usage_dict
from coderai.utils.common.validate import repair_json_string


class CompletionStream(Protocol):
    def __iter__(self) -> Iterator[Any]: ...


def assemble_stream_response(
    resp: CompletionStream,
    reasoning_key: str | None,
    *,
    on_chunk: Callable[[str], None] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    on_thinking_chunk: Callable[[str], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Consume deltas in order, retaining callback, cancellation and usage behavior."""
    content_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls_dict: dict[int, dict[str, Any]] = {}
    refusal_parts: list[str] = []
    usage_dict: dict[str, int] = {}
    estimated_tokens = 0

    try:
        for chunk in resp:
            if is_cancelled and is_cancelled():
                if hasattr(resp, "close"):
                    try:
                        resp.close()
                    except Exception:
                        pass
                from coderai.soul.session.manager import SessionInterrupted

                raise SessionInterrupted("Streaming cancelled by user")

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
                                on_progress({"estimatedTokens": estimated_tokens, "type": "update"})
                            except Exception:
                                pass

                    delta_thinking = extract_reasoning_content(delta, reasoning_key) or getattr(
                        delta, "thinking", None
                    )
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
        from coderai.soul.session.manager import SessionInterrupted

        if isinstance(stream_err, SessionInterrupted):
            raise stream_err
        if thinking_parts:
            setattr(stream_err, "partial_thinking", "".join(str(p) for p in thinking_parts))
        if content_parts:
            setattr(stream_err, "partial_content", "".join(str(p) for p in content_parts))
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
