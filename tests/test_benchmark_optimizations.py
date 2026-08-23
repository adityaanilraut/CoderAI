"""Unit and integration tests for benchmark and latency optimizations."""

import asyncio
import json
import pytest

from coderai.core.common.message_converter import OpenAIMessageConverter
from coderai.core.common.model_capabilities import (
    resolve_adaptive_reasoning_effort,
    defaults_to_thinking_mode,
    is_fast_model,
)
from coderai.core.prompt import format_tool_definitions, get_runtime_context
from coderai.core.prompt_sections import order_tools
from coderai.core.session import SessionManager, SessionMessage


def test_deterministic_tool_ordering():
    """Verify tool ordering remains 100% deterministic regardless of input list permutations."""
    tools = [
        {"type": "function", "function": {"name": "read", "description": "Read file"}},
        {"type": "function", "function": {"name": "bash", "description": "Run bash"}},
        {"type": "function", "function": {"name": "edit", "description": "Edit file"}},
        {"type": "function", "function": {"name": "grep", "description": "Search file"}},
        {"type": "function", "function": {"name": "glob", "description": "Find files"}},
    ]
    perm1 = [tools[4], tools[0], tools[2], tools[1], tools[3]]
    perm2 = [tools[1], tools[3], tools[0], tools[4], tools[2]]

    res1 = format_tool_definitions(perm1)
    res2 = format_tool_definitions(perm2)

    names1 = [t["function"]["name"] for t in res1]
    names2 = [t["function"]["name"] for t in res2]

    assert names1 == names2
    assert json.dumps(res1, sort_keys=True) == json.dumps(res2, sort_keys=True)


def test_runtime_context_static_prefix():
    """Verify runtime context can suppress volatile dates for frozen prompt caching."""
    ctx_dynamic = get_runtime_context("/test/root", model="deepseek-v4-flash", suppress_dynamic_time=False)
    ctx_static = get_runtime_context("/test/root", model="deepseek-v4-flash", suppress_dynamic_time=True)

    assert "Today is" in ctx_dynamic
    assert "Today is" not in ctx_static
    assert "Current LLM model: deepseek-v4-flash." in ctx_static
    assert "/test/root" in ctx_static


def test_adaptive_reasoning_effort():
    """Verify adaptive reasoning scales down on iterative steps for fast/flash models."""
    # Flash model (adaptive)
    assert resolve_adaptive_reasoning_effort("deepseek-v4-flash", turn=1, step=1) == "high"
    assert resolve_adaptive_reasoning_effort("deepseek-v4-flash", turn=2, step=1) == "low"
    assert resolve_adaptive_reasoning_effort("deepseek-v4-flash", turn=1, step=2) == "low"

    # Flagship model (adaptive)
    assert resolve_adaptive_reasoning_effort("deepseek-v4-pro", turn=1, step=1) == "max"
    assert resolve_adaptive_reasoning_effort("deepseek-v4-pro", turn=2, step=1) == "high"

    # Explicit override is preserved
    assert resolve_adaptive_reasoning_effort("deepseek-v4-flash", turn=2, step=1, explicit_effort="max") == "max"


def test_message_converter_safe_empty_and_pruning():
    """Verify empty outputs and tool result pruning behavior."""
    converter = OpenAIMessageConverter()

    # 1. Empty assistant message with tools
    msg_asst = SessionMessage(
        id="1",
        session_id="s1",
        role="assistant",
        content=None,
        tool_calls=[{"id": "call_1", "function": {"name": "read", "arguments": "{}"}}],
        thinking="Planning step",
    )
    conv_asst = converter._convert_message(msg_asst, thinking_enabled=True, model="deepseek-v4-flash")
    assert conv_asst["content"] == ""
    assert conv_asst["reasoning_content"] == "Planning step"
    assert len(conv_asst["tool_calls"]) == 1

    # 2. Empty tool result
    msg_tool_empty = SessionMessage(
        id="2",
        session_id="s1",
        role="tool",
        content="",
        tool_call_id="call_1",
    )
    conv_tool_empty = converter._convert_message(msg_tool_empty, thinking_enabled=True, model="deepseek-v4-flash")
    assert conv_tool_empty["content"] == "(no output)"

    # 3. Oversized tool result pruning
    large_text = "A" * 20_000
    msg_tool_large = SessionMessage(
        id="3",
        session_id="s1",
        role="tool",
        content=large_text,
        tool_call_id="call_1",
    )
    conv_tool_large = converter._convert_message(
        msg_tool_large, thinking_enabled=True, model="deepseek-v4-flash", max_tool_result_chars=1000
    )
    assert len(conv_tool_large["content"]) < 20_000
    assert "characters omitted" in conv_tool_large["content"]
    assert conv_tool_large["content"].startswith("A" * 500)
    assert conv_tool_large["content"].endswith("A" * 500)


@pytest.mark.asyncio
async def test_session_parallel_read_only_tool_execution(tmp_path):
    """Verify that contiguous read-only tool calls execute concurrently and commit in order."""
    file1 = tmp_path / "f1.txt"
    file2 = tmp_path / "f2.txt"
    file1.write_text("Hello from 1", encoding="utf-8")
    file2.write_text("Hello from 2", encoding="utf-8")

    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": None, "model": "gpt-5.6-luna"},
        get_resolved_settings=lambda: {"model": "gpt-5.6-luna"},
    )
    session_id = await mgr.create_session("Initial prompt")
    mgr._update_entry(session_id, lambda e: {**e, "status": "processing"})

    tool_calls = [
        {
            "id": "c1",
            "type": "function",
            "function": {"name": "read", "arguments": json.dumps({"file_path": "f1.txt"})},
        },
        {
            "id": "c2",
            "type": "function",
            "function": {"name": "read", "arguments": json.dumps({"file_path": "f2.txt"})},
        },
    ]

    waiting = await mgr._append_tool_messages(session_id, tool_calls)
    assert waiting is False

    messages = mgr.list_session_messages(session_id)
    tool_messages = [m for m in messages if m.role == "tool"]

    assert len(tool_messages) == 2
    assert tool_messages[0].tool_call_id == "c1"
    assert "Hello from 1" in tool_messages[0].content
    assert tool_messages[1].tool_call_id == "c2"
    assert "Hello from 2" in tool_messages[1].content
