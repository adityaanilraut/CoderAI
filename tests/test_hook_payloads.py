"""Compatibility contracts for lifecycle event payload entry points."""

from __future__ import annotations

import inspect
import asyncio
import shlex
import subprocess
import sys

import pytest

from coderai import hooks
from coderai.hooks import events, runner
from coderai.utils.subprocess_env import is_process_alive


def _python_hook_command(tmp_path, script):
    path = tmp_path / "hook_command.py"
    path.write_text(script, encoding="utf-8")
    argv = [sys.executable, str(path)]
    return subprocess.list2cmdline(argv) if sys.platform == "win32" else shlex.join(argv)


@pytest.mark.parametrize("point", ["PreTurn", "PostTurn", "Stop", "SubagentStart"])
async def test_turn_and_child_hooks_cancel_without_blocking_or_leaking_processes(
    tmp_path, monkeypatch, point
):
    from coderai.cli.session_factory import close_session_manager
    from coderai.soul.coderaisoul import AgentLoop
    from coderai.soul.session.manager import SessionManager
    from coderai.subagents.builder import SubAgentSpec
    from coderai.subagents.core import AgentRegistry
    from coderai.subagents.runner import SubAgentManager

    pid_file = tmp_path / "lifecycle-hook.pid"
    script = (
        "import os, pathlib, time; "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(30)"
    )
    command = _python_hook_command(tmp_path, script)
    monkeypatch.setattr(
        "coderai.hooks.engine.load_hook_config",
        lambda *_a, **_kw: {point: [{"command": command}]},
    )
    manager = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": object()},
        get_resolved_settings=lambda: {},
    )
    sid = await manager.create_empty_session()
    loop = AgentLoop(manager, sid)
    registry = AgentRegistry()
    monkeypatch.setattr(sys.modules["coderai.subagents.core"], "_registry", registry)
    child_manager = SubAgentManager(str(tmp_path), lambda: {"client": object()})
    spec = SubAgentSpec("hook probe", "wait", parent_session_id=sid)
    if point == "SubagentStart":
        task = asyncio.create_task(child_manager.spawn_subagent(spec))
    elif point == "PreTurn":
        task = asyncio.create_task(loop.run())
    else:
        task = asyncio.create_task(loop.emit_turn_end_async("natural"))
    try:
        async with asyncio.timeout(3):
            while not pid_file.exists():
                await asyncio.sleep(0.01)
        pid = int(pid_file.read_text())
        assert not task.done()
        if point == "SubagentStart":
            child_manager.cancel_subagent(registry.get(spec.task_id).run_session_id)
            assert (await asyncio.wait_for(task, 3)).status == "interrupted"
            assert not child_manager._active_controllers
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
            assert sid not in manager.session_controllers
        assert not is_process_alive(pid)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await close_session_manager(manager)


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


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["PreToolUse", "PostToolUse"])
async def test_tool_hooks_leave_event_loop_responsive_and_cancel_process(
    tmp_path, monkeypatch, point
):
    from coderai.tools.legacy.executor import ToolExecutor
    from coderai.tools.legacy.registry import ToolRegistry
    from coderai.tools.legacy.types import ToolDefinition, ToolResult

    pid_file = tmp_path / "hook.pid"
    script = (
        "import os, pathlib, time; "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(30)"
    )
    command = _python_hook_command(tmp_path, script)
    monkeypatch.setattr(
        "coderai.hooks.engine.load_hook_config",
        lambda *_a, **_kw: {point: [{"matcher": "probe", "command": command}]},
    )
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(name="probe", handler=lambda _args, _ctx: ToolResult(ok=True, name="probe"))
    )
    executor = ToolExecutor(str(tmp_path), registry=registry)
    call = {"id": "hook-probe", "function": {"name": "probe", "arguments": "{}"}}
    task = asyncio.create_task(executor.execute_tool_call("hook", call))
    pid = None
    try:
        async with asyncio.timeout(2):
            while not pid_file.exists():
                await asyncio.sleep(0.01)
        pid = int(pid_file.read_text())
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with asyncio.timeout(2):
            while is_process_alive(pid):
                await asyncio.sleep(0.01)
    finally:
        if not task.done():
            task.cancel()
        if pid is not None:
            from coderai.utils.subprocess_env import kill_process_tree

            kill_process_tree(pid)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["engine", "runner"])
async def test_async_hook_cancellation_propagates_and_reaps_descendant(tmp_path, entry):
    from coderai.hooks.engine import execute_hook_command_async

    pid_file = tmp_path / "hook.pid"
    script = (
        "import os, pathlib, time; "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(30)"
    )
    command = _python_hook_command(tmp_path, script)
    if entry == "engine":
        invocation = execute_hook_command_async(command, {}, str(tmp_path))
    else:
        invocation = runner.run_hook(command, {}, cwd=str(tmp_path))
    task = asyncio.create_task(invocation)
    try:
        async with asyncio.timeout(2):
            while not pid_file.exists():
                await asyncio.sleep(0.01)
        pid = int(pid_file.read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        async with asyncio.timeout(2):
            while is_process_alive(pid):
                await asyncio.sleep(0.01)
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_sync_hook_timeout_reaps_descendant(tmp_path):
    from coderai.hooks.engine import execute_hook_command

    pid_file = tmp_path / "hook.pid"
    script = (
        "import os, pathlib, time; "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(30)"
    )
    command = _python_hook_command(tmp_path, script)
    result = await asyncio.to_thread(execute_hook_command, command, {}, str(tmp_path), 0.2)
    assert result.decision == "deny" and "timed out" in result.reason
    pid = int(pid_file.read_text())
    async with asyncio.timeout(2):
        while is_process_alive(pid):
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_async_hook_preparation_preserves_duplicate_binding_and_stop_guarantees(
    tmp_path, monkeypatch
):
    from unittest.mock import AsyncMock
    from coderai.hooks import MergedHookOutcome
    from coderai.tools.legacy.authorization import prepare_pre_tool_outcomes_async
    from coderai.tools.legacy.types import matches_tool_call_binding, tool_call_binding

    duplicate = {"id": "duplicate", "function": {"name": "read", "arguments": "{}"}}
    first = {"id": "first", "function": {"name": "read", "arguments": '{"file_path":"a.txt"}'}}
    later = {"id": "later", "function": {"name": "write", "arguments": "{}"}}
    hook = AsyncMock(return_value=MergedHookOutcome(stop=True, stop_reason="halt"))
    monkeypatch.setattr("coderai.hooks.run_hook_point_async", hook)
    snapshots = await prepare_pre_tool_outcomes_async(
        "hooks", str(tmp_path), [duplicate, duplicate, first, later], {}
    )
    assert list(snapshots) == ["first"] and hook.await_count == 1
    assert matches_tool_call_binding(
        snapshots["first"], tool_call_binding("hooks", str(tmp_path), first)
    )
    assert snapshots["first"]["outcome"]["stopReason"] == "halt"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_async_hook_wire_events_bracket_success_and_cancellation(
    tmp_path, monkeypatch, cancel
):
    from unittest.mock import AsyncMock
    from coderai.hooks.engine import run_hook_point_async
    from coderai.hooks import HookOutput
    from coderai.wire.emitter import WireEmitter
    from coderai.wire.types import HookResolved, HookTriggered

    wire = WireEmitter()
    monkeypatch.setattr("coderai.wire.emitter.get_emitter", lambda: wire)
    seen = []
    monkeypatch.setattr(wire, "send", seen.append)
    started = asyncio.Event()

    async def run(**_kwargs):
        started.set()
        if cancel:
            await asyncio.Event().wait()
        return HookOutput(decision="allow")

    monkeypatch.setattr(
        "coderai.hooks.engine.execute_hook_command_async", AsyncMock(side_effect=run)
    )
    task = asyncio.create_task(
        run_hook_point_async(
            "PreToolUse",
            {"tool_name": "probe"},
            str(tmp_path),
            settings={"hooks": {"PreToolUse": [{"command": "probe"}]}},
        )
    )
    await asyncio.wait_for(started.wait(), 1)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert (await task).decision == "allow"
    assert (
        len(seen) == 2 and isinstance(seen[0], HookTriggered) and isinstance(seen[1], HookResolved)
    )
    assert seen[1].target == "probe"
    assert seen[1].action == ("block" if cancel else "allow")
    assert seen[1].duration_ms >= 0
