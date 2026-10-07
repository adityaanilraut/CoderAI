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


@pytest.mark.parametrize("streaming", [True, False])
def test_mandatory_reasoning_400_retries_without_mutating_request(streaming):
    import copy

    import httpx
    from openai import BadRequestError

    from coderai.soul.session.completion import call_sync

    sent = []
    response = {"choices": [{"message": {"content": "Hi"}}]}
    error = BadRequestError(
        "Reasoning is mandatory for this endpoint and cannot be disabled.",
        response=httpx.Response(
            400, request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
        ),
        body={
            "error": {"message": "Reasoning is mandatory for this endpoint and cannot be disabled."}
        },
    )

    def create(**kwargs):
        sent.append(kwargs)
        if len(sent) == 1:
            raise error
        return response

    client = NS(base_url="https://openrouter.ai/api/v1", chat=NS(completions=NS(create=create)))
    request = {
        "model": "liquid/lfm-2.5-2.6b:free",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 2048,
        "extra_body": {"reasoning": {"enabled": False}, "provider": {"sort": "latency"}},
    }
    original = copy.deepcopy(request)
    run = call_stream_or_sync if streaming else call_sync
    assert run(client, request) == response
    assert len(sent) == 2
    assert sent[1]["extra_body"] == {
        "reasoning": {"enabled": True, "exclude": True},
        "provider": {"sort": "latency"},
    }
    assert sent[1]["messages"] == original["messages"]
    assert sent[1]["max_tokens"] == 2048
    assert request == original


@pytest.mark.parametrize(
    "host,status,reasoning",
    [
        ("openrouter.ai", 400, {"enabled": True}),
        ("openrouter.ai", 401, {"enabled": False}),
        ("example.com", 400, {"enabled": False}),
    ],
)
def test_mandatory_reasoning_retry_does_not_mask_other_failures(host, status, reasoning):
    import httpx
    from openai import APIStatusError

    calls = []
    error = APIStatusError(
        "Reasoning is mandatory for this endpoint and cannot be disabled.",
        response=httpx.Response(
            status, request=httpx.Request("POST", f"https://{host}/api/v1/chat/completions")
        ),
        body=None,
    )

    def create(**kwargs):
        calls.append(kwargs)
        raise error

    client = NS(base_url=f"https://{host}/api/v1", chat=NS(completions=NS(create=create)))
    with pytest.raises(APIStatusError) as caught:
        call_stream_or_sync(client, {"model": "test", "extra_body": {"reasoning": reasoning}})
    assert caught.value is error
    assert len(calls) == 1


def test_mandatory_reasoning_retry_is_bounded():
    import httpx
    from openai import BadRequestError

    calls = []
    error = BadRequestError(
        "Reasoning is mandatory for this endpoint and cannot be disabled.",
        response=httpx.Response(
            400, request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
        ),
        body=None,
    )

    def create(**kwargs):
        calls.append(kwargs)
        raise error

    client = NS(base_url="https://openrouter.ai/api/v1", chat=NS(completions=NS(create=create)))
    with pytest.raises(BadRequestError):
        call_stream_or_sync(
            client, {"model": "test", "extra_body": {"reasoning": {"enabled": False}}}
        )
    assert len(calls) == 2


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
    assert result["_usage_source"] == "provider-reported"


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
    assert result["_usage_source"] == "estimated"


def test_stream_timing_ignores_empty_deltas_and_records_reasoning_then_tool(monkeypatch):
    times = iter([10.0, 12.0])
    monkeypatch.setattr("coderai.soul.session.streaming.time.time", lambda: next(times))
    result = call_stream_or_sync(
        _client(
            Stream(
                [
                    _chunk(content=""),
                    _chunk(reasoning_content="thinking"),
                    _chunk(content="answer"),
                    _chunk(
                        tool_calls=[NS(index=0, id="c", function=NS(name="read", arguments="{}"))]
                    ),
                ]
            )
        ),
        {"model": "test"},
    )
    assert result["_stream_timing"] == {"first_token_at": 10.0, "first_tool_call_at": 12.0}
