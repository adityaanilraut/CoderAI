import json
from unittest.mock import AsyncMock

import pytest

from coderAI.llm.anthropic import AnthropicProvider
from coderAI.tui.streaming import BridgeStreamingHandler


class _Content:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._sent = False

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if self._sent:
            raise StopAsyncIteration
        self._sent = True
        return self._payload


class _Response:
    status = 200

    def __init__(self, events: list[tuple[str, dict]]) -> None:
        wire = "".join(f"event: {event}\ndata: {json.dumps(data)}\n\n" for event, data in events)
        self.content = _Content(wire.encode())

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None


class _Server:
    def emit(self, _event: str, **_data) -> None:
        pass


@pytest.mark.asyncio
async def test_anthropic_stream_emits_tool_name_once():
    provider = AnthropicProvider("claude-sonnet-4-6", api_key="test")
    events = [
        ("message_start", {"message": {"usage": {"input_tokens": 3}}}),
        (
            "content_block_start",
            {
                "index": 0,
                "content_block": {"type": "tool_use", "id": "tool_1", "name": "read_file"},
            },
        ),
        (
            "content_block_delta",
            {"index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"pa'}},
        ),
        (
            "content_block_delta",
            {"index": 0, "delta": {"type": "input_json_delta", "partial_json": 'th":"x"}'}},
        ),
        ("message_delta", {"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 5}}),
        ("message_stop", {}),
    ]
    provider._post_to_anthropic = AsyncMock(return_value=_Response(events))

    chunks = [chunk async for chunk in provider.stream([{"role": "user", "content": "read"}])]
    argument_chunks = [
        call
        for chunk in chunks
        for choice in chunk.get("choices", [])
        for call in choice.get("delta", {}).get("tool_calls", [])
        if call.get("function", {}).get("arguments")
    ]
    assert all(call["function"]["name"] == "" for call in argument_chunks)

    result = await BridgeStreamingHandler(_Server()).handle_stream(_as_stream(chunks))
    assert result["tool_calls"][0]["function"] == {
        "name": "read_file",
        "arguments": '{"path":"x"}',
    }
    assert result["finish_reason"] == "tool_calls"


@pytest.mark.asyncio
async def test_anthropic_stream_maps_max_tokens_to_length():
    provider = AnthropicProvider("claude-sonnet-4-6", api_key="test")
    provider._post_to_anthropic = AsyncMock(
        return_value=_Response(
            [
                ("message_start", {"message": {"usage": {}}}),
                ("message_delta", {"delta": {"stop_reason": "max_tokens"}, "usage": {}}),
                ("message_stop", {}),
            ]
        )
    )

    chunks = [chunk async for chunk in provider.stream([{"role": "user", "content": "x"}])]

    assert chunks[-1]["choices"][0]["finish_reason"] == "length"


def test_non_streaming_thinking_blocks_round_trip_with_tool_use():
    provider = AnthropicProvider("claude-sonnet-4-6", api_key="test")
    thinking_blocks = [
        {"type": "thinking", "thinking": "summary", "signature": "opaque-signature"},
        {"type": "redacted_thinking", "data": "opaque-redacted-data"},
    ]
    response = provider._convert_response(
        {
            "content": [
                *thinking_blocks,
                {"type": "tool_use", "id": "tool_1", "name": "read_file", "input": {}},
            ],
            "stop_reason": "tool_use",
        }
    )

    payload = provider._build_payload(
        [
            {"role": "user", "content": "read"},
            response["choices"][0]["message"],
            {"role": "tool", "tool_call_id": "tool_1", "content": "done"},
        ],
        reasoning_effort="none",
    )

    assert payload["messages"][1]["content"][:2] == thinking_blocks
    assert payload["messages"][1]["content"][2]["type"] == "tool_use"


@pytest.mark.asyncio
async def test_streaming_signature_round_trips_with_tool_use():
    provider = AnthropicProvider("claude-sonnet-4-6", api_key="test")
    events = [
        ("message_start", {"message": {"usage": {}}}),
        (
            "content_block_start",
            {
                "index": 0,
                "content_block": {"type": "thinking", "thinking": "", "signature": ""},
            },
        ),
        (
            "content_block_delta",
            {"index": 0, "delta": {"type": "thinking_delta", "thinking": "summary"}},
        ),
        (
            "content_block_delta",
            {"index": 0, "delta": {"type": "signature_delta", "signature": "opaque"}},
        ),
        ("content_block_stop", {"index": 0}),
        (
            "content_block_start",
            {
                "index": 1,
                "content_block": {"type": "tool_use", "id": "tool_1", "name": "read_file"},
            },
        ),
        (
            "content_block_delta",
            {"index": 1, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
        ),
        ("content_block_stop", {"index": 1}),
        ("message_delta", {"delta": {"stop_reason": "tool_use"}, "usage": {}}),
        ("message_stop", {}),
    ]
    provider._post_to_anthropic = AsyncMock(return_value=_Response(events))

    chunks = [chunk async for chunk in provider.stream([{"role": "user", "content": "read"}])]
    result = await BridgeStreamingHandler(_Server()).handle_stream(_as_stream(chunks))
    payload = provider._build_payload(
        [
            {"role": "user", "content": "read"},
            {"role": "assistant", **result},
            {"role": "tool", "tool_call_id": "tool_1", "content": "done"},
        ],
        reasoning_effort="none",
    )

    assert payload["messages"][1]["content"][0] == {
        "type": "thinking",
        "thinking": "summary",
        "signature": "opaque",
    }


def test_anthropic_adaptive_thinking_and_per_call_disable():
    provider = AnthropicProvider("opus", api_key="test", reasoning_effort="high")

    enabled = provider._build_payload([{"role": "user", "content": "x"}])
    disabled = provider._build_payload([{"role": "user", "content": "x"}], reasoning_effort="none")

    assert enabled["thinking"] == {"type": "adaptive"}
    assert enabled["output_config"] == {"effort": "high"}
    assert "thinking" not in disabled
    assert "output_config" not in disabled


async def _as_stream(chunks):
    for chunk in chunks:
        yield chunk
