"""Characterize child policy precedence through the complete isolated loop."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from coderai.subagents.builder import SubAgentSpec
from coderai.subagents.runner import SubAgentManager


@pytest.mark.parametrize(
    ("name", "policy", "args", "denial"),
    [
        ("read", {"mode": "general"}, {}, None),
        ("write", {"mode": "general"}, {"file_path": "a.txt"}, None),
        ("read", {"mode": "general", "exclude_tools": ["read"]}, {}, "excluded"),
        ("read", {"mode": "general", "allowed_tools": []}, {}, "allowlist"),
        (
            "bash",
            {"mode": "read_only", "sandbox_mode": "workspace-write"},
            {},
            "outside read-only sandbox",
        ),
        ("bash", {"mode": "read_only", "sandbox_mode": "read-only"}, {}, None),
        ("write", {"mode": "read_only"}, {}, "mutating tool"),
        ("terminal_read", {"mode": "read_only"}, {}, "mutating tool"),
        ("Task", {"mode": "read_only"}, {"mode": "general"}, "writable child"),
        ("Task", {"mode": "read_only"}, {"mode": "read_only"}, None),
        ("Task", {"mode": "read_only"}, {}, "writable child"),
    ],
)
async def test_child_policy_and_execution_context(
    tmp_path, monkeypatch, name, policy, args, denial
):
    seen = []
    after = []
    call = {
        "id": "tool",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }
    responses = iter(
        [
            {"choices": [{"message": {"tool_calls": [call]}}]},
            {"choices": [{"message": {"content": "done"}}]},
        ]
    )
    client = NS(chat=NS(completions=NS(create=lambda **kwargs: next(responses))))
    manager = SubAgentManager(
        str(tmp_path),
        create_openai_client=lambda: {"client": client, "model": "test"},
        get_resolved_settings=lambda: {},
    )
    manager._inherits_permission_settings = False

    class Executor:
        def __init__(self, *args):
            pass

        async def execute_tool_calls(self, sid, calls, hooks):
            seen.append((sid, calls, hooks))
            if name == "write":
                hooks.on_after_file_mutation("a.txt")
            return [{"content": "executed"}]

    monkeypatch.setattr("coderai.tools.legacy.executor.ToolExecutor", Executor)
    spec = SubAgentSpec(
        description="test",
        prompt="test",
        isolated_cwd=str(tmp_path),
        dry_run=True,
        on_after_file_mutation=after.append,
        **policy,
    )
    state = {}
    result = await manager._run_subagent_loop(
        spec,
        "session",
        asyncio.Event(),
        messages=[{"role": "user", "content": "start"}],
        state=state,
    )
    assert result.status == "completed" and result.summary == "done"
    tool_message = next(message for message in state["messages"] if message["role"] == "tool")
    if denial:
        assert denial in json.loads(tool_message["content"])["error"]
        assert seen == []
    else:
        assert tool_message["content"] == "executed" and len(seen) == 1
        hooks = seen[0][2]
        assert hooks.dry_run and hooks.isolated_cwd == str(tmp_path)
        assert hooks.allowed_tools == spec.allowed_tools and hooks.sandbox_mode == spec.sandbox_mode
        assert hooks.should_stop() is False
    if name == "write" and denial is None:
        assert after == ["a.txt"] and result.diffs == [{"file_path": "a.txt"}]
