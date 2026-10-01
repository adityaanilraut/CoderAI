"""Provider stream assembly: ordering, partial failures and cancellation."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from coderai.soul.session.completion import call_stream_or_sync
from coderai.soul.session.manager import SessionInterrupted


class Stream:
    def __init__(self, chunks, error=None):
        self.chunks = chunks
        self.error = error
        self.closed = False

    def __iter__(self):
        yield from self.chunks
        if self.error:
            raise self.error

    def close(self):
        self.closed = True


def _chunk(**fields):
    return NS(choices=[NS(delta=NS(**fields))], usage=None)


def _client(stream):
    return NS(chat=NS(completions=NS(create=lambda **kwargs: stream)))


def test_stream_assembly_preserves_callback_order_and_tool_fragments():
    stream = Stream(
        [
            _chunk(
                content="Hi",
                thinking="why",
                tool_calls=[NS(index=1, id="b", function=NS(name="re", arguments='{"x":'))],
            ),
            _chunk(
                content="!",
                refusal="no",
                tool_calls=[
                    NS(index=1, id="b", function=NS(name="ad", arguments="1}")),
                    NS(index=0, id="a", function=NS(name="think", arguments="{}")),
                ],
            ),
            NS(choices=[], usage={"prompt_tokens": 2, "completion_tokens": 4, "total_tokens": 6}),
        ]
    )
    seen = []
    result = call_stream_or_sync(
        _client(stream),
        {"model": "test"},
        on_chunk=lambda text: seen.append(("text", text)),
        on_thinking_chunk=lambda text: seen.append(("thinking", text)),
        on_progress=lambda data: seen.append(("progress", data)),
    )
    assert seen == [
        ("text", "Hi"),
        ("progress", {"estimatedTokens": 1, "type": "update"}),
        ("thinking", "why"),
        ("progress", {"estimatedTokens": 2, "type": "update", "isThinking": True}),
        ("text", "!"),
        ("progress", {"estimatedTokens": 3, "type": "update"}),
        ("progress", {"estimatedTokens": 3, "type": "end"}),
    ]
    message = result["choices"][0]["message"]
    assert (
        message["content"] == "Hi!"
        and message["reasoning_content"] == "why"
        and message["refusal"] == "no"
    )
    assert message["tool_calls"] == [
        {"id": "a", "type": "function", "function": {"name": "think", "arguments": "{}"}},
        {"id": "b", "type": "function", "function": {"name": "read", "arguments": '{"x":1}'}},
    ]
    assert result["usage"]["total_tokens"] == 6


def test_stream_failure_attaches_partial_content_without_end_notification():
    error = RuntimeError("provider failed")
    progress = []
    stream = Stream([_chunk(content="partial", thinking="thought")], error)
    with pytest.raises(RuntimeError, match="provider failed") as caught:
        call_stream_or_sync(_client(stream), {"model": "test"}, on_progress=progress.append)
    assert caught.value is error
    assert error.partial_content == "partial" and error.partial_thinking == "thought"
    assert all(item["type"] != "end" for item in progress)


def test_stream_cancellation_closes_response_and_propagates():
    stream = Stream([_chunk(content="first"), _chunk(content="second")])
    delivered = []
    with pytest.raises(SessionInterrupted, match="Streaming cancelled by user"):
        call_stream_or_sync(
            _client(stream),
            {"model": "test"},
            on_chunk=delivered.append,
            is_cancelled=lambda: bool(delivered),
        )
    assert delivered == ["first"] and stream.closed


def test_callback_failure_does_not_abort_stream():
    def broken(*args):
        raise RuntimeError("renderer failed")

    result = call_stream_or_sync(
        _client(Stream([_chunk(content="text", thinking="thought")])),
        {"model": "test"},
        on_chunk=broken,
        on_thinking_chunk=broken,
        on_progress=broken,
    )
    assert result["choices"][0]["message"]["content"] == "text"
    assert result["usage"]["completion_tokens"] == 2
