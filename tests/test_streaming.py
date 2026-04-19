"""Tests for the Ink IPC streaming handler.

The Rich-based ``StreamingHandler`` was removed when the interactive UI
migrated to Ink; ``IPCStreamingHandler`` replaces it and honors the same
``handle_stream`` contract, so the same shape of tests applies.
"""

import asyncio

from coderAI.ipc.streaming import IPCStreamingHandler


class _FakeServer:
    """Minimal stand-in for ``IPCServer`` that records emitted events."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event: str, **data) -> None:
        self.events.append((event, data))


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
        # Each content delta should have been relayed as a stream_delta event.
        deltas = [
            d["content"]
            for name, d in server.events
            if name == "stream_delta" and not d.get("reasoning")
        ]
        assert "".join(deltas) == "Hello World"

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

        asyncio.run(run())
        reasoning = "".join(
            d["content"]
            for name, d in server.events
            if name == "stream_delta" and d.get("reasoning")
        )
        regular = "".join(
            d["content"]
            for name, d in server.events
            if name == "stream_delta" and not d.get("reasoning")
        )
        assert "inner thoughts" in reasoning
        assert "before " in regular
        assert " after" in regular
