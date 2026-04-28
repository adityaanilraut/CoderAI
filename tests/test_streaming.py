"""Tests for the Ink IPC streaming handler.

The Rich-based ``StreamingHandler`` was removed when the interactive UI
migrated to Ink; ``IPCStreamingHandler`` replaces it and honors the same
``handle_stream`` contract, so the same shape of tests applies.

The streaming handler emits a single phased ``turn`` event:
``("turn", phase="start" | "reasoning" | "text" | "end", delta?)``. These
helpers extract the text/reasoning streams in that shape so the assertions
read like the old ``stream_delta`` ones.
"""

import asyncio

from coderAI.ipc.streaming import IPCStreamingHandler


class _FakeServer:
    """Minimal stand-in for ``IPCServer`` that records emitted events."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event: str, **data) -> None:
        self.events.append((event, data))


def _text_deltas(events):
    return "".join(
        d.get("delta", "")
        for name, d in events
        if name == "turn" and d.get("phase") == "text"
    )


def _reasoning_deltas(events):
    return "".join(
        d.get("delta", "")
        for name, d in events
        if name == "turn" and d.get("phase") == "reasoning"
    )


async def _fake_stream(chunks):
    for chunk in chunks:
        yield chunk


class TestIPCStreamingHandler:
    """Contract tests: content accumulation, tool-call merging, emit events."""

    def test_empty_stream(self):
        handler = IPCStreamingHandler(_FakeServer())

        async def run():
            return await handler.handle_stream(_fake_stream([]))

        result = asyncio.run(run())
        assert isinstance(result, dict)
        assert "content" in result or "tool_calls" in result

    def test_content_chunks(self):
        server = _FakeServer()
        handler = IPCStreamingHandler(server)

        chunks = [
            {"choices": [{"delta": {"content": "Hello"}}]},
            {"choices": [{"delta": {"content": " World"}}]},
            {"choices": [{"delta": {}}]},
        ]

        async def run():
            return await handler.handle_stream(_fake_stream(chunks))

        result = asyncio.run(run())
        assert result.get("content") == "Hello World"
        # Each content delta should have been relayed as a turn/text event.
        assert _text_deltas(server.events) == "Hello World"

    def test_ignores_duplicate_cumulative_resend(self):
        """Provider sometimes emits the same full `content` twice; must not double UI text."""
        server = _FakeServer()
        handler = IPCStreamingHandler(server)
        text = "How can I help?"
        chunks = [
            {"choices": [{"delta": {"content": text}}]},
            {"choices": [{"delta": {"content": text}}]},
        ]

        async def run():
            return await handler.handle_stream(_fake_stream(chunks))

        result = asyncio.run(run())
        assert result.get("content") == text
        assert _text_deltas(server.events) == text

    def test_tool_call_accumulation(self):
        handler = IPCStreamingHandler(_FakeServer())

        chunks = [
            {
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "id": "call_1",
                            "function": {"name": "read_file", "arguments": '{"pa'},
                        }]
                    }
                }]
            },
            {
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "function": {"arguments": 'th": "x.py"}'},
                        }]
                    }
                }]
            },
            {"choices": [{"delta": {}}]},
        ]

        async def run():
            return await handler.handle_stream(_fake_stream(chunks))

        result = asyncio.run(run())
        tool_calls = result.get("tool_calls")
        assert tool_calls is not None
        assert len(tool_calls) == 1
        assert tool_calls[0]["function"]["name"] == "read_file"
        assert '"path": "x.py"' in tool_calls[0]["function"]["arguments"]

    def test_mixed_content_and_tool_calls(self):
        handler = IPCStreamingHandler(_FakeServer())

        chunks = [
            {"choices": [{"delta": {"content": "Let me check"}}]},
            {
                "choices": [{
                    "delta": {
                        "content": None,
                        "tool_calls": [{
                            "index": 0,
                            "id": "call_2",
                            "function": {"name": "git_status", "arguments": "{}"},
                        }],
                    }
                }]
            },
            {"choices": [{"delta": {}}]},
        ]

        async def run():
            return await handler.handle_stream(_fake_stream(chunks))

        result = asyncio.run(run())
        assert result.get("content") is not None
        assert result.get("tool_calls") is not None

    def test_reasoning_tags_split(self):
        """``<think>…</think>`` regions surface as reasoning-flagged deltas."""
        server = _FakeServer()
        handler = IPCStreamingHandler(server)

        chunks = [
            {"choices": [{"delta": {"content": "before <think>inner"}}]},
            {"choices": [{"delta": {"content": " thoughts</think> after"}}]},
            {"choices": [{"delta": {}}]},
        ]

        async def run():
            return await handler.handle_stream(_fake_stream(chunks))

        result = asyncio.run(run())
        assert "inner thoughts" in _reasoning_deltas(server.events)
        regular = _text_deltas(server.events)
        assert "before " in regular
        assert " after" in regular
        # Final content lives on the handler's return value (the turn/end
        # event is now content-free — see streaming.py).
        assert result["content"] == "before  after"

    def test_reasoning_field_stays_out_of_final_content(self):
        server = _FakeServer()
        handler = IPCStreamingHandler(server)

        chunks = [
            {"choices": [{"delta": {"reasoning_content": "plan"}}]},
            {"choices": [{"delta": {"content": "answer"}}]},
            {"choices": [{"delta": {}}]},
        ]

        async def run():
            return await handler.handle_stream(_fake_stream(chunks))

        result = asyncio.run(run())
        assert result["content"] == "answer"
        assert _text_deltas(server.events) == "answer"

    def test_cumulative_think_chunks_do_not_duplicate_reasoning(self):
        server = _FakeServer()
        handler = IPCStreamingHandler(server)

        chunks = [
            {"choices": [{"delta": {"content": "before <think>inner"}}]},
            {
                "choices": [{
                    "delta": {
                        "content": "before <think>inner thoughts</think> after",
                    }
                }]
            },
            {"choices": [{"delta": {}}]},
        ]

        async def run():
            return await handler.handle_stream(_fake_stream(chunks))

        result = asyncio.run(run())
        assert result["content"] == "before  after"
        assert _reasoning_deltas(server.events) == "inner thoughts"

    def test_delta_then_cumulative_does_not_duplicate(self):
        """Provider flips from pure delta to cumulative mid-stream.

        Once a cumulative chunk lands, only the suffix past what was already
        seen must be re-emitted — not the whole prefix again.
        """
        server = _FakeServer()
        handler = IPCStreamingHandler(server)

        chunks = [
            {"choices": [{"delta": {"content": "Hello"}}]},            # delta
            {"choices": [{"delta": {"content": "Hello, world"}}]},     # cumulative
            {"choices": [{"delta": {"content": "!"}}]},                # delta again
            {"choices": [{"delta": {}}]},
        ]

        async def run():
            return await handler.handle_stream(_fake_stream(chunks))

        result = asyncio.run(run())
        assert result["content"] == "Hello, world!"
        assert _text_deltas(server.events) == "Hello, world!"

    def test_cumulative_then_delta_does_not_duplicate(self):
        """Provider starts cumulative, then switches to pure deltas."""
        server = _FakeServer()
        handler = IPCStreamingHandler(server)

        chunks = [
            {"choices": [{"delta": {"content": "Hello, "}}]},           # first cumulative == content
            {"choices": [{"delta": {"content": "Hello, world"}}]},      # cumulative
            {"choices": [{"delta": {"content": "!"}}]},                 # delta
            {"choices": [{"delta": {}}]},
        ]

        async def run():
            return await handler.handle_stream(_fake_stream(chunks))

        result = asyncio.run(run())
        assert result["content"] == "Hello, world!"
        assert _text_deltas(server.events) == "Hello, world!"

    def test_literal_angle_bracket_flushes_promptly(self):
        """A ``<`` that can't possibly lead into ``<think>`` must not be held.

        Regression for the buffered-lookahead bug: any ``<`` used to freeze
        the entire tail of the buffer until enough characters arrived to
        disprove a ``<think>`` match, causing visible jitter for content like
        ``<T>`` or ``<html>``.
        """
        server = _FakeServer()
        handler = IPCStreamingHandler(server)

        chunks = [
            {"choices": [{"delta": {"content": "use <T> as a type"}}]},
            {"choices": [{"delta": {}}]},
        ]

        async def run():
            return await handler.handle_stream(_fake_stream(chunks))

        result = asyncio.run(run())
        assert result["content"] == "use <T> as a type"
        assert _text_deltas(server.events) == "use <T> as a type"
