"""Provider stream assembly independent of client negotiation and sync fallback."""

from __future__ import annotations

import uuid
import time
import threading
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
    close_stream: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Consume deltas in order, retaining callback, cancellation and usage behavior."""
    content_parts: list[str] = []
    thinking_parts: list[str] = []
    reasoning_details: list[Any] = []
    tool_calls_dict: dict[int, dict[str, Any]] = {}
    refusal_parts: list[str] = []
    usage_dict: dict[str, int] = {}
    estimated_tokens = 0
    first_token_at: float | None = None
    first_tool_call_at: float | None = None

    close = close_stream or getattr(resp, "close", None)
    finished = threading.Event()
    closed = threading.Event()
    close_lock = threading.Lock()

    def close_owned_stream() -> None:
        with close_lock:
            if closed.is_set() or not callable(close):
                return
            closed.set()
        try:
            close()
        except Exception:
            pass

    watcher = None
    if is_cancelled is not None and callable(close):

        def watch_cancellation() -> None:
            while not finished.wait(0.05):
                if is_cancelled():
                    close_owned_stream()
                    return

        watcher = threading.Thread(target=watch_cancellation, daemon=True)
        watcher.start()
    try:
        for chunk in resp:
            if is_cancelled and is_cancelled():
                close_owned_stream()
                from coderai.soul.session.manager import SessionInterrupted

                raise SessionInterrupted("Streaming cancelled by user")

            choices = getattr(chunk, "choices", None) or []
            if choices:
                c = choices[0]
                delta = getattr(c, "delta", None)
                if delta:
                    if any(
                        getattr(delta, key, None)
                        for key in (
                            "content",
                            "tool_calls",
                            "reasoning_content",
                            "thinking",
                            reasoning_key or "reasoning_content",
                        )
                    ):
                        if first_token_at is None:
                            first_token_at = time.time()
                    if getattr(delta, "tool_calls", None) and first_tool_call_at is None:
                        first_tool_call_at = time.time()
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

                    # OpenRouter streams reasoning blocks here; replay them
                    # verbatim next turn so the model continues where it left off.
                    delta_details = getattr(delta, "reasoning_details", None)
                    if delta_details:
                        blocks = (
                            delta_details if isinstance(delta_details, list) else [delta_details]
                        )
                        reasoning_details.extend(blocks)

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

        if is_cancelled and is_cancelled():
            raise SessionInterrupted("Streaming cancelled by user") from stream_err
        if isinstance(stream_err, SessionInterrupted):
            raise stream_err
        if thinking_parts:
            setattr(stream_err, "partial_thinking", "".join(str(p) for p in thinking_parts))
        if content_parts:
            setattr(stream_err, "partial_content", "".join(str(p) for p in content_parts))
        raise stream_err
    finally:
        finished.set()
        close_owned_stream()
        if watcher is not None:
            watcher.join(timeout=0.2)

    if on_progress:
        try:
            on_progress({"estimatedTokens": estimated_tokens, "type": "end"})
        except Exception:
            pass

    usage_source = "provider-reported" if usage_dict else "unavailable"
    if not usage_dict and estimated_tokens > 0:
        usage_source = "estimated"
        usage_dict = {
            "prompt_tokens": 0,
            "completion_tokens": estimated_tokens,
            "total_tokens": estimated_tokens,
            "cached_tokens": 0,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 0,
        }

    if is_cancelled and is_cancelled():
        from coderai.soul.session.manager import SessionInterrupted

        raise SessionInterrupted("Streaming cancelled by user")

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
                    "reasoning_details": reasoning_details or None,
                    "refusal": "".join(refusal_parts) or None,
                }
            }
        ]
    }
    if usage_dict:
        res["usage"] = usage_dict
    res["_usage_source"] = usage_source
    res["_stream_timing"] = {
        "first_token_at": first_token_at,
        "first_tool_call_at": first_tool_call_at,
    }
    return res
