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


async def _as_stream(chunks):
    for chunk in chunks:
        yield chunk
