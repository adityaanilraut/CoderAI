"""Compatibility contracts for lifecycle event payload entry points."""

from __future__ import annotations

import inspect

import pytest

from coderai import hooks
from coderai.hooks import events, runner


@pytest.mark.parametrize(
    ("name", "event", "fields", "defaults"),
    [
        (
            "pre_tool_use",
            "PreToolUse",
            {"tool_name": "read", "tool_input": {"path": "é"}},
            {"tool_call_id": ""},
        ),
        (
            "post_tool_use",
            "PostToolUse",
            {"tool_name": "read", "tool_input": {}},
            {"tool_output": "", "tool_call_id": ""},
        ),
        (
            "post_tool_use_failure",
            "PostToolUseFailure",
            {"tool_name": "read", "tool_input": {}, "error": "failed"},
            {"tool_call_id": ""},
        ),
        ("user_prompt_submit", "UserPromptSubmit", {"prompt": "hello"}, {}),
        ("stop", "Stop", {}, {"stop_hook_active": False}),
        ("stop_failure", "StopFailure", {"error_type": "Timeout", "error_message": "expired"}, {}),
        ("session_start", "SessionStart", {"source": "resume"}, {}),
        ("session_end", "SessionEnd", {"reason": "exit"}, {}),
        ("subagent_start", "SubagentStart", {"agent_name": "planner", "prompt": "plan"}, {}),
        ("subagent_stop", "SubagentStop", {"agent_name": "planner"}, {"response": ""}),
        ("pre_compact", "PreCompact", {"trigger": "auto", "token_count": 123}, {}),
        ("post_compact", "PostCompact", {"trigger": "auto", "estimated_token_count": 42}, {}),
        (
            "notification",
            "Notification",
            {"sink": "terminal", "notification_type": "done"},
            {"title": "", "body": "", "severity": "info"},
        ),
    ],
)
def test_payload_names_preserve_fields_and_defaults(name, event, fields, defaults):
    canonical = getattr(events, name)
    compatibility = getattr(runner, f"build_{name}_payload")
    exported = getattr(hooks, f"build_{name}_payload")
    assert inspect.signature(canonical) == inspect.signature(compatibility)
    common = {"session_id": "session-1", "cwd": "/workspace/é"}
    expected = {"hook_event_name": event, **common, **fields, **defaults}
    for build in (canonical, compatibility, exported):
        assert build(**common, **fields) == expected
        explicit = {
            key: True if isinstance(value, bool) else f"explicit-{key}"
            for key, value in defaults.items()
        }
        assert build(**common, **fields, **explicit) == {**expected, **explicit}
