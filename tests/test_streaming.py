"""Tests for the StreamingHandler."""

import asyncio
from unittest.mock import patch

import pytest

from coderAI.ui.streaming import StreamingHandler


async def _fake_stream(chunks):
    """Helper that yields content chunks like an LLM stream."""
    for chunk in chunks:
        yield chunk


class TestStreamingHandler:
    """Tests for StreamingHandler.handle_stream."""

    def test_empty_stream(self):
        handler = StreamingHandler()

        async def run():
            return await handler.handle_stream(_fake_stream([]))

        result = asyncio.run(run())
        # Should return a dict with at least content key
        assert isinstance(result, dict)
        assert "content" in result or "tool_calls" in result

    def test_content_chunks(self):
        handler = StreamingHandler()

        chunks = [
            {"choices": [{"delta": {"content": "Hello"}}]},
            {"choices": [{"delta": {"content": " World"}}]},
            {"choices": [{"delta": {}}]},  # empty delta = end
        ]

        async def run():
            return await handler.handle_stream(_fake_stream(chunks))

        result = asyncio.run(run())
        assert result.get("content") == "Hello World"

    def test_tool_call_accumulation(self):
        handler = StreamingHandler()

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
        handler = StreamingHandler()

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
