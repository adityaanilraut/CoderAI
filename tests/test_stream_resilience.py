"""Unit tests for streaming resilience, token estimation, and repetition loop sanitization."""

from coderai.cli.app import _StreamState
from coderai.core.session import _call_stream_or_sync, sanitize_repetition_loops


def test_sanitize_repetition_loops_collapses_degenerate_patterns():
    corrupted_pattern = (
        "- Parses the main te- Parses the main te- Parses the main te- Parses the main te- "
        "- Parses the main te- Parses the main te- fi- Parses the main te- Parses the main te- "
        "- Parses the main te- Parses the main te- Parses the main te- Parses the main te- "
        "- Command-line argument parsing."
    )
    sanitized = sanitize_repetition_loops(corrupted_pattern)
    assert "[truncated repetition loop]" in sanitized
    assert "- Command-line argument parsing." in sanitized


def test_sanitize_repetition_loops_preserves_normal_content():
    normal_markdown = (
        "## Summary\n"
        "- Step 1: Install dependencies\n"
        "- Step 2: Run test suite\n"
        "- Step 3: Verify build output\n"
        "- Step 4: Deploy application\n"
    )
    assert sanitize_repetition_loops(normal_markdown) == normal_markdown


def test_stream_state_ensure_newline(capsys):
    state = _StreamState()
    assert state.had_streamed() is False
    assert state.ensure_newline() is False

    state.on_chunk("Generating text...")
    assert state.had_streamed() is True

    # Calling ensure_newline should flush a newline and reset streaming state
    flushed = state.ensure_newline()
    assert flushed is True
    assert state.had_streamed() is False

    captured = capsys.readouterr()
    assert captured.out == "Generating text...\n"


def test_stream_state_reasoning_deduplication():
    from coderai.cli.app import _StreamState

    state = _StreamState()
    assert state.thinking_rendered is False

    state.on_thinking_chunk("Analyzing database migrations...")
    assert state.thinking_streamer.is_active is True
    assert state.thinking_rendered is False

    # When first content chunk arrives, thinking streamer finalizes and marks thinking_rendered
    state.on_chunk("Migration plan ready.")
    assert state.thinking_streamer.is_active is False
    assert state.thinking_rendered is True

    # Reset clears thinking_rendered
    state.reset()
    assert state.thinking_rendered is False


def test_stream_state_thinking_only_completion():
    from coderai.cli.app import _StreamState

    state = _StreamState()

    state.on_thinking_chunk("Reasoning about next step...")
    assert state.thinking_streamer.is_active is True
    assert state.thinking_rendered is False

    # Calling ensure_newline finalizes thinking if no content chunks arrived
    state.ensure_newline()
    assert state.thinking_streamer.is_active is False
    assert state.thinking_rendered is True


def test_call_stream_or_sync_passes_stream_options():
    called_requests = []

    class MockChunk:
        def __init__(self, content=None, usage=None):
            self.choices = (
                [
                    type(
                        "Choice",
                        (),
                        {
                            "delta": type(
                                "Delta",
                                (),
                                {
                                    "content": content,
                                    "reasoning_content": None,
                                    "refusal": None,
                                    "tool_calls": None,
                                },
                            )()
                        },
                    )
                ]
                if content
                else []
            )
            self.usage = type("Usage", (), usage)() if usage else None

    class MockCompletions:
        def create(self, **kwargs):
            called_requests.append(kwargs)
            return iter(
                [
                    MockChunk(content="Hello "),
                    MockChunk(content="world!"),
                    MockChunk(
                        usage={"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}
                    ),
                ]
            )

    class MockClient:
        chat = type("Chat", (), {"completions": MockCompletions()})()

    res = _call_stream_or_sync(MockClient(), {"model": "gpt-4o", "messages": []})
    assert len(called_requests) == 1
    req = called_requests[0]
    assert req.get("stream") is True
    assert req.get("stream_options") == {"include_usage": True}
    usage = res.get("usage") or {}
    assert usage.get("prompt_tokens") == 12
    assert usage.get("completion_tokens") == 5
    assert usage.get("total_tokens") == 17
    assert res["choices"][0]["message"]["content"] == "Hello world!"


def test_call_stream_or_sync_fallback_token_estimation():
    class MockChunk:
        def __init__(self, content=None):
            self.choices = (
                [
                    type(
                        "Choice",
                        (),
                        {
                            "delta": type(
                                "Delta",
                                (),
                                {
                                    "content": content,
                                    "reasoning_content": None,
                                    "refusal": None,
                                    "tool_calls": None,
                                },
                            )()
                        },
                    )
                ]
                if content
                else []
            )
            self.usage = None

    class MockCompletions:
        def create(self, **kwargs):
            return iter(
                [
                    MockChunk(content="A" * 40),
                ]
            )

    class MockClient:
        chat = type("Chat", (), {"completions": MockCompletions()})()

    res = _call_stream_or_sync(MockClient(), {"model": "gpt-4o", "messages": []})
    assert res.get("usage") is not None
    assert res["usage"]["total_tokens"] > 0
    assert res["choices"][0]["message"]["content"] == "A" * 40
