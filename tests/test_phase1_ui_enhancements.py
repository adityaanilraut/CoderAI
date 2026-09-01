"""Tests for Phase 1 UI Enhancements: Live Thinking Streaming, Pre-Approval Diffs, and Risk Badges."""

from __future__ import annotations

import pathlib
import tempfile

from coderai.cli.app import _prompt_permissions
from coderai.cli.thinking import LiveThinkingStreamer, summarize_thinking
from coderai.core.permissions import (
    _generate_file_diff_preview,
    compute_tool_call_permissions,
    describe_tool_permission_request,
    get_request_risk_badge,
    get_scope_risk_level,
)
from coderai.core.session import _call_stream_or_sync


def test_live_thinking_streamer_lifecycle():
    streamer = LiveThinkingStreamer()
    assert not streamer.is_active
    assert streamer.start_time is None

    streamer.on_chunk("First reasoning chunk. ")
    assert streamer.is_active
    assert streamer.start_time is not None
    assert len(streamer.thinking_chunks) == 1

    streamer.on_chunk("Second reasoning chunk.")
    assert len(streamer.thinking_chunks) == 2

    # Finalize
    final_text = streamer.finalize()
    assert "First reasoning chunk. Second reasoning chunk." == final_text
    assert not streamer.is_active
    assert len(streamer.thinking_chunks) == 0


def test_live_thinking_streamer_terminal_width_bounds(monkeypatch):
    from unittest.mock import MagicMock
    import sys

    # Mock terminal width to 60 columns
    mock_console = MagicMock()
    mock_console.width = 60
    mock_console.is_terminal = True

    streamer = LiveThinkingStreamer(mock_console)
    assert streamer._get_term_width() == 60

    written_lines = []
    monkeypatch.setattr(sys.stdout, "write", lambda s: written_lines.append(s))
    monkeypatch.setattr(sys.stdout, "flush", lambda: None)

    # Feed very long reasoning text
    long_reasoning = "The site renders correctly: 26 menu cards, 6 category tabs, cart count 0. Everything works. Let me also test the interactive behavior via browser..."
    streamer.on_chunk(long_reasoning)
    streamer._render_inline()

    assert len(written_lines) > 0
    # Verify no line overflow
    assert streamer._last_line_len <= 60


def test_summarize_thinking():
    short_text = "Analyzing module dependencies..."
    assert summarize_thinking(short_text) == short_text

    long_text = "word " * 100
    summary = summarize_thinking(long_text, max_chars=50)
    assert len(summary) <= 50
    assert summary.endswith("...")


def test_risk_level_ratings():
    assert get_scope_risk_level("read-in-cwd") == "low"
    assert get_scope_risk_level("write-in-cwd") == "moderate"
    assert get_scope_risk_level("write-out-cwd") == "high"
    assert get_scope_risk_level("delete-out-cwd") == "critical"
    assert get_scope_risk_level("mutate-git-log") == "critical"

    label, style = get_request_risk_badge(["read-in-cwd"])
    assert "LOW" in label

    label, style = get_request_risk_badge(["write-in-cwd"])
    assert "MODERATE" in label

    label, style = get_request_risk_badge(["write-out-cwd"])
    assert "HIGH" in label

    label, style = get_request_risk_badge(["delete-out-cwd"])
    assert "CRITICAL" in label


def test_generate_file_diff_preview_write():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = pathlib.Path(tmpdir)
        test_file = tmp_path / "hello.py"
        test_file.write_text("print('hello')", encoding="utf-8")

        diff = _generate_file_diff_preview(
            str(tmp_path), "hello.py", None, None, full_new_content="print('world')\n"
        )
        assert diff is not None
        assert "-print('hello')" in diff
        assert "+print('world')" in diff


def test_generate_file_diff_preview_edit():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = pathlib.Path(tmpdir)
        test_file = tmp_path / "calc.py"
        test_file.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")

        diff = _generate_file_diff_preview(str(tmp_path), "calc.py", "return a - b", "return a + b")
        assert diff is not None
        assert "-    return a - b" in diff
        assert "+    return a + b" in diff


def test_describe_tool_permission_request_attaches_diff():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = pathlib.Path(tmpdir)
        test_file = tmp_path / "main.py"
        test_file.write_text("v = 1\n", encoding="utf-8")

        tool_call = {
            "id": "tc_1",
            "type": "function",
            "function": {
                "name": "write",
                "arguments": '{"file_path": "main.py", "content": "v = 2\\n"}',
            },
        }
        req = describe_tool_permission_request(
            session_id="sess_1",
            project_root=str(tmp_path),
            tool_call=tool_call,
        )
        assert "diff_preview" in req
        assert "-v = 1" in req["diff_preview"]
        assert "+v = 2" in req["diff_preview"]


def test_evaluate_tool_permissions_includes_diff_and_risk():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = pathlib.Path(tmpdir)
        test_file = tmp_path / "app.py"
        test_file.write_text("x = 10\n", encoding="utf-8")

        tool_calls = [
            {
                "id": "tc_edit",
                "type": "function",
                "function": {
                    "name": "edit",
                    "arguments": '{"file_path": "app.py", "old_str": "x = 10", "new_str": "x = 20"}',
                },
            }
        ]
        # Force ask mode
        settings = {"allow": [], "deny": [], "ask": ["write-in-cwd"], "defaultMode": "askAll"}
        plan = compute_tool_call_permissions(
            session_id="sess_1",
            project_root=str(tmp_path),
            tool_calls=tool_calls,
            settings=settings,
        )
        ask_list = plan["askPermissions"]
        assert len(ask_list) == 1
        assert ask_list[0]["toolCallId"] == "tc_edit"
        assert ask_list[0]["diff_preview"] is not None
        assert "-x = 10" in ask_list[0]["diff_preview"]
        assert "+x = 20" in ask_list[0]["diff_preview"]
        assert "risk_level" in ask_list[0]


def test_prompt_permissions_auto_approve():
    requests = [
        {
            "toolCallId": "tc_1",
            "name": "write",
            "command": "write test.py",
            "scopes": ["write-in-cwd"],
            "risk_level": "MODERATE RISK",
            "diff_preview": "--- a/test.py\n+++ b/test.py\n@@ -1 +1 @@\n-old\n+new",
        }
    ]
    replies, always = _prompt_permissions(requests, yes=True)
    assert len(replies) == 1
    assert replies[0]["permission"] == "allow"
    assert "write-in-cwd" in always


def test_session_manager_thinking_chunk_callback():
    thinking_received: list[str] = []

    def on_thinking(chunk: str) -> None:
        thinking_received.append(chunk)

    # Test _call_stream_or_sync with mock chunks
    class MockDelta:
        def __init__(self, content=None, thinking=None):
            self.content = content
            self.reasoning_content = thinking
            self.refusal = None
            self.tool_calls = None

    class MockChoice:
        def __init__(self, delta):
            self.delta = delta

    class MockChunk:
        def __init__(self, delta):
            self.choices = [MockChoice(delta)]

    class MockStream:
        def __iter__(self):
            yield MockChunk(MockDelta(thinking="Let's analyze "))
            yield MockChunk(MockDelta(thinking="the code."))
            yield MockChunk(MockDelta(content="Hello!"))

    class MockClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    return MockStream()

    res = _call_stream_or_sync(
        MockClient(),
        {"model": "gpt-5.6-luna", "messages": []},
        on_thinking_chunk=on_thinking,
    )

    assert "".join(thinking_received) == "Let's analyze the code."
    msg = res["choices"][0]["message"]
    assert msg["content"] == "Hello!"
    assert msg["reasoning_content"] == "Let's analyze the code."
