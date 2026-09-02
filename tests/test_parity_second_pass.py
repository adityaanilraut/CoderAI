"""Comprehensive tests for Second-Pass Parity & Hardening.

Validates:
1. Deterministic Prompt & Tool Canonicalization for maximum KV cache hits.
2. POSIX device permissions in Seatbelt profile & Sandbox validation.
3. Full Lifecycle Hook stages (pre_tool_call, post_tool_call, on_tool_error, pre_turn, post_turn, on_subagent_spawn) & alias resolution.
4. Streaming resilience & partial thinking preservation on failure.
5. CLI rich tool cards for session query and code mode.
"""

from __future__ import annotations

import json
from rich.console import Console

from coderai.cli.tool_card import render_tool_card
from coderai.core.hooks import (
    HookPoint,
    normalize_hook_point,
    run_post_tool_use,
    run_on_tool_error,
    run_pre_turn,
    run_post_turn,
    run_on_subagent_spawn,
)
from coderai.core.prompt import (
    build_cache_stabilized_messages,
    TOOL_GUIDANCE_MAP,
)
from coderai.core.prompt_sections import TOOL_ORDER
from coderai.core.sandbox import (
    build_seatbelt_profile,
    check_sandbox_path_access,
)
from coderai.core.session import SessionMessage, _call_stream_or_sync
from coderai.core.tools.types import ToolExecutionContext, ToolResult


# --- 1. Prompt Assembly & KV Cache Stabilization Tests ---


def test_tool_order_covers_all_registered_tools():
    """Verify that TOOL_ORDER explicitly includes essential built-in and extended tools."""
    expected_tools = [
        "bash",
        "glob",
        "grep",
        "read",
        "write",
        "edit",
        "skill",
        "Task",
        "subagent",
        "session_query",
        "session_search",
        "session_trace",
        "session_event_search",
        "session_event_read",
        "code_mode",
        "lsp",
        "pwsh",
    ]
    for t in expected_tools:
        assert t in TOOL_ORDER, f"Tool '{t}' missing from deterministic TOOL_ORDER"


def test_build_cache_stabilized_messages_deterministic():
    """Verify system message prefix stabilization with cache control and canonical tool order."""
    sys_prompt = "You are a helpful software engineer."
    messages = [
        {"role": "system", "content": "Old prompt"},
        {"role": "user", "content": "Fix bug"},
    ]
    tools = [
        {"type": "function", "function": {"name": "write", "description": "Write file"}},
        {"type": "function", "function": {"name": "bash", "description": "Run shell"}},
    ]
    stabilized_msgs, stabilized_tools = build_cache_stabilized_messages(
        messages, sys_prompt, tools=tools, include_boundary_tag=True, enable_cache_control=True
    )
    assert stabilized_msgs[0]["role"] == "system"
    assert "CODERAI_KV_CACHE_PREFIX_BOUNDARY" in stabilized_msgs[0]["content"]
    assert stabilized_msgs[0]["cache_control"] == {"type": "ephemeral"}
    assert stabilized_msgs[1]["role"] == "user"
    assert stabilized_msgs[1]["content"] == "Fix bug"

    # Verify tool ordering: bash precedes write in canonical TOOL_ORDER
    tool_names = [t["function"]["name"] for t in stabilized_tools]
    assert tool_names == ["bash", "write"]


def test_tool_guidance_map_coverage():
    """Verify session tools have registered guidance documentation."""
    assert "session_search" in TOOL_GUIDANCE_MAP
    assert "session_trace" in TOOL_GUIDANCE_MAP
    assert "session_event_search" in TOOL_GUIDANCE_MAP
    assert "session_event_read" in TOOL_GUIDANCE_MAP


# --- 2. Sandbox & Permissions Tests ---


def test_seatbelt_profile_contains_standard_devices(tmp_path):
    """Verify Seatbelt profile permits standard POSIX terminal devices."""
    profile = build_seatbelt_profile("workspace-write", str(tmp_path))
    assert '(allow file-write-data (literal "/dev/null"))' in profile
    assert '(allow file-write-data (literal "/dev/zero"))' in profile
    assert '(allow file-write-data (literal "/dev/urandom"))' in profile
    assert '(allow file-write-data (literal "/dev/random"))' in profile
    assert '(allow file-write-data (literal "/dev/tty"))' in profile
    assert '(allow file-write-data (literal "/dev/ptmx"))' in profile
    assert '(allow file-write-data (literal "/dev/stdout"))' in profile
    assert '(allow file-write-data (literal "/dev/stderr"))' in profile


def test_sandbox_path_validation_escapes(tmp_path):
    """Verify path traversal outside workspace is blocked under workspace-write."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    outside = tmp_path / "outside.txt"

    ok, err = check_sandbox_path_access(
        outside, op="write", mode="workspace-write", workspace_root=ws
    )
    assert ok is False
    assert "SANDBOX_VIOLATION" in (err or "")

    inside = ws / "inside.txt"
    ok, err = check_sandbox_path_access(
        inside, op="write", mode="workspace-write", workspace_root=ws
    )
    assert ok is True
    assert err is None


# --- 3. Lifecycle Hooks & Interceptors Tests ---


def test_hook_point_aliases_normalization():
    """Verify hook point aliases map cleanly to canonical names."""
    assert normalize_hook_point("pre_tool_call") == "PreToolUse"
    assert normalize_hook_point("post_tool_call") == "PostToolUse"
    assert normalize_hook_point("on_tool_error") == "ToolError"
    assert normalize_hook_point("pre_turn") == "PreTurn"
    assert normalize_hook_point("post_turn") == "PostTurn"
    assert normalize_hook_point("on_subagent_spawn") == "SubagentSpawn"
    assert normalize_hook_point(HookPoint.PRE_TOOL_USE) == "PreToolUse"


def test_lifecycle_hooks_execution(tmp_path):
    """Verify execution of pre_turn, post_turn, and subagent hooks."""
    settings = {
        "hooks": {
            "pre_turn": [
                {"command": 'echo \'{"decision": "allow", "additionalContext": ["TurnContext"]}\''}
            ],
            "post_turn": [{"command": 'echo \'{"decision": "allow"}\''}],
            "on_subagent_spawn": [{"command": 'echo \'{"decision": "allow"}\''}],
        }
    }
    pre_res = run_pre_turn(1, "test_session", str(tmp_path), settings=settings)
    assert pre_res.is_allowed()
    assert "TurnContext" in pre_res.additional_context

    post_res = run_post_turn(
        1, "test_session", str(tmp_path), reason="completed", settings=settings
    )
    assert post_res.is_allowed()

    spawn_res = run_on_subagent_spawn(
        "root_session", "sub_1", "Explore repo", project_root=str(tmp_path), settings=settings
    )
    assert spawn_res.is_allowed()


def test_post_tool_use_and_tool_error_hooks(tmp_path):
    """Verify post_tool_use and on_tool_error hooks trigger with correct payloads."""
    settings = {
        "hooks": {
            "post_tool_call": [
                {"command": 'echo \'{"decision": "allow", "additionalContext": ["PostToolDoc"]}\''}
            ],
            "on_tool_error": [
                {"command": 'echo \'{"decision": "deny", "reason": "Handled tool error"}\''}
            ],
        }
    }
    ctx = ToolExecutionContext(session_id="s1", project_root=str(tmp_path))
    post_res = run_post_tool_use(
        "bash",
        {"command": "ls"},
        ToolResult(ok=True, name="bash", output="file.txt"),
        ctx,
        settings=settings,
    )
    assert post_res.is_allowed()
    assert "PostToolDoc" in post_res.additional_context

    err_res = run_on_tool_error(
        "bash", {"command": "invalid"}, "Command failed", ctx, settings=settings
    )
    assert err_res.decision == "deny"


# --- 4. Stream Resilience & Partial Thinking Preservation ---


def test_stream_error_preserves_partial_thinking_and_content():
    """Verify that network drops mid-stream preserve accumulated partial thinking and content on the exception."""

    class FailingStream:
        def __iter__(self):
            chunk1 = type(
                "Chunk",
                (),
                {
                    "choices": [
                        type(
                            "Choice",
                            (),
                            {
                                "delta": type(
                                    "Delta",
                                    (),
                                    {
                                        "content": None,
                                        "reasoning_content": "Deep thought...",
                                        "refusal": None,
                                        "tool_calls": None,
                                    },
                                )()
                            },
                        )()
                    ],
                    "usage": None,
                },
            )()
            chunk2 = type(
                "Chunk",
                (),
                {
                    "choices": [
                        type(
                            "Choice",
                            (),
                            {
                                "delta": type(
                                    "Delta",
                                    (),
                                    {
                                        "content": "Hello ",
                                        "reasoning_content": None,
                                        "refusal": None,
                                        "tool_calls": None,
                                    },
                                )()
                            },
                        )()
                    ],
                    "usage": None,
                },
            )()
            yield chunk1
            yield chunk2
            raise ConnectionResetError("Connection dropped by peer")

    class MockCompletions:
        def create(self, **kwargs):
            return FailingStream()

    class MockClient:
        chat = type("Chat", (), {"completions": MockCompletions()})()

    captured_err = None
    try:
        _call_stream_or_sync(MockClient(), {"model": "gpt-4o", "messages": []})
    except Exception as exc:
        captured_err = exc

    assert captured_err is not None
    assert isinstance(captured_err, ConnectionResetError)
    assert getattr(captured_err, "partial_thinking", "") == "Deep thought..."
    assert getattr(captured_err, "partial_content", "") == "Hello "


# --- 5. Terminal & CLI Tool Card Rendering ---


def test_cli_session_query_card_render():
    """Verify that session_query tool results render cleanly with the rich card formatter."""
    msg = SessionMessage(
        id="m1",
        session_id="s1",
        role="tool",
        content=json.dumps(
            {
                "ok": True,
                "name": "session_query",
                "output": "Found 3 turns in session history",
                "metadata": {"query": "authentication", "count": 3},
            }
        ),
    )
    console = Console(record=True, color_system="truecolor")
    render_tool_card(console, msg)
    text = console.export_text()
    assert "session_query" in text
    assert "authentication" in text
    assert "3 events" in text


def test_cli_code_mode_card_render():
    """Verify that code_mode tool results render with python badges."""
    msg = SessionMessage(
        id="m2",
        session_id="s1",
        role="tool",
        content=json.dumps(
            {
                "ok": True,
                "name": "code_mode",
                "output": "42\nExecution complete",
                "metadata": {"durationMs": 12.4},
            }
        ),
    )
    console = Console(record=True, color_system="truecolor")
    render_tool_card(console, msg)
    text = console.export_text()
    assert "code_mode" in text
    assert "Python Code Mode" in text
    assert "42" in text
