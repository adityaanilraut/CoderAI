"""Reference-derived guarantees for cancellation, streaming and child policies."""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import nullcontext
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from rich.console import Console

from coderai.background.agent_runner import spawn_background_agent
from coderai.orchestration import get_orchestration_event_bus
from coderai.subagents.builder import SubAgentSpec, SubagentDescriptor, ToolRestriction
from coderai.subagents.core import AgentRegistry
from coderai.subagents.runner import SubAgentManager
from coderai.tools.agent import handle_subagent_tool
from coderai.tools.legacy.types import ToolExecutionContext
from coderai.ui.shell.visualize._subagents import SubagentActivityBlock
from coderai.wire.emitter import WireEmitter, bind_emitter, reset_emitter
from coderai.wire.types import SubagentEvent, TextPart, ToolResultPart


@pytest.fixture
def registry(monkeypatch):
    registry = AgentRegistry()
    monkeypatch.setattr("coderai.subagents.core._registry", registry)
    return registry


def _manager(tmp_path, responses):
    responses = iter(responses)
    client = NS(chat=NS(completions=NS(create=lambda **kwargs: next(responses))))
    return SubAgentManager(str(tmp_path), lambda: {"client": client, "model": "test"})


def _response(content="", tool_calls=None, finish_reason="stop"):
    return {
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {
                    "content": content,
                    "tool_calls": tool_calls,
                },
            }
        ]
    }


async def test_cancel_all_drains_blocked_turn(tmp_path, monkeypatch, registry):
    manager = _manager(tmp_path, [])
    started, drained = asyncio.Event(), asyncio.Event()

    async def blocked(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    monkeypatch.setattr(manager, "_run_subagent_loop", blocked)
    task = asyncio.create_task(manager.spawn_subagent(SubAgentSpec("blocked", "wait")))
    await started.wait()
    manager.cancel_all()
    result = await asyncio.wait_for(task, 0.5)
    assert result.status == "interrupted"
    assert drained.is_set()
    assert not manager._active_controllers


async def test_parent_cancellation_has_terminal_event(tmp_path, monkeypatch, registry):
    manager = _manager(tmp_path, [])
    started = asyncio.Event()

    async def blocked(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(manager, "_run_subagent_loop", blocked)
    events = []

    def listener(name, payload):
        events.append((name, payload))

    bus = get_orchestration_event_bus()
    bus.subscribe(listener)
    try:
        spec = SubAgentSpec("blocked", "wait")
        task = asyncio.create_task(manager.spawn_subagent(spec))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        lifecycle = [(name, payload) for name, payload in events if name != "subagent/activity"]
        assert [name for name, _ in lifecycle] == ["subagent/start", "subagent/end"]
        assert lifecycle[-1][1]["stopReason"] == "aborted"
        assert registry.get(spec.task_id).status == "interrupted"
    finally:
        bus.unsubscribe(listener)


async def test_interrupted_continuable_history_can_resume(tmp_path, monkeypatch, registry):
    calls = [
        {"id": cid, "function": {"name": "read", "arguments": "{}"}} for cid in ("first", "second")
    ]
    manager = _manager(tmp_path, [_response(tool_calls=calls), _response("recovered")])
    started = asyncio.Event()

    async def blocked(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("coderai.tools.legacy.executor.ToolExecutor.execute_tool_calls", blocked)
    spec = SubAgentSpec("continuable", "read", parent_session_id="parent")
    handle = await spawn_background_agent(manager, spec)
    try:
        await started.wait()
        # A separate manager can abort a continuable turn without killing
        # its worker, even while the child waits inside a tool.
        _manager(tmp_path, []).cancel_subagent(handle.run_session_id)

        async def settled():
            while handle.status == "running":
                await asyncio.sleep(0)

        await asyncio.wait_for(settled(), 0.5)
        assert handle.status == "interrupted"
        tool_results = [msg for msg in handle.conversation if msg["role"] == "tool"]
        assert {msg["tool_call_id"] for msg in tool_results} == {"first", "second"}
        registry.send(handle.id, "continue")

        async def recovered():
            while handle.result is None or handle.result.summary != "recovered":
                await asyncio.sleep(0)

        await asyncio.wait_for(recovered(), 0.5)
        assert handle.status == "completed"
    finally:
        registry.kill(handle.id)
        await handle.task


async def test_live_child_deltas_are_wrapped_and_closed(tmp_path, registry):
    class Stream:
        closed = False

        def __iter__(self):
            for text in ("hello ", "world"):
                yield NS(choices=[NS(delta=NS(content=text), finish_reason=None)], usage=None)
            yield NS(choices=[NS(delta=None, finish_reason="stop")], usage=None)

        def close(self):
            self.closed = True

    stream = Stream()
    manager = _manager(tmp_path, [stream])
    emitter = WireEmitter()
    token = bind_emitter(emitter)
    try:
        result = await manager.spawn_subagent(
            SubAgentSpec(
                "stream",
                "stream",
                parent_tool_call_id="parent-call",
            )
        )
        assert result.summary == "hello world" and stream.closed
        events = emitter.buffered()
        assert events and all(isinstance(event, SubagentEvent) for event in events)
        assert [event.event.text for event in events if isinstance(event.event, TextPart)] == [
            "hello ",
            "world",
        ]
        assert all(event.parent_tool_call_id == "parent-call" for event in events)
    finally:
        reset_emitter(token)
        emitter.close()


@pytest.mark.parametrize(
    "response,status",
    [
        (_response(""), "failed"),
        (_response("partial", finish_reason="length"), "budget_exceeded"),
    ],
)
async def test_incomplete_handoff_cannot_succeed(tmp_path, registry, response, status):
    result = await _manager(tmp_path, [response]).spawn_subagent(SubAgentSpec("handoff", "finish"))
    assert result.status == status
    assert result.error


async def test_descriptor_filter_enforced_during_execution(tmp_path, monkeypatch, registry):
    call = {"id": "read", "function": {"name": "read", "arguments": "{}"}}
    manager = _manager(tmp_path, [_response(tool_calls=[call]), _response("done")])
    execute = AsyncMock()
    monkeypatch.setattr("coderai.tools.legacy.executor.ToolExecutor.execute_tool_calls", execute)
    result = await manager.spawn_subagent(
        SubAgentSpec(
            "restrict",
            "read",
            mode="general",
            descriptor=SubagentDescriptor(tool_filter=ToolRestriction(deny=["read"])),
        )
    )
    assert result.status == "completed"
    execute.assert_not_called()
    conversation = registry.get(result.task_id).conversation
    error = json.loads(next(msg["content"] for msg in conversation if msg["role"] == "tool"))[
        "error"
    ]
    assert "descriptor policy" in error


async def test_task_handler_inherits_parent_permission_resolver(tmp_path, monkeypatch):
    seen = []

    async def spawn(manager, spec):
        seen.append(manager.get_resolved_settings())
        assert manager._inherits_permission_settings
        from coderai.subagents.output import SubAgentResult

        return SubAgentResult(spec.task_id, "child", "completed", "done")

    monkeypatch.setattr(SubAgentManager, "spawn_subagent", spawn)
    policy = {"permissions": {"write": "deny"}}
    context = ToolExecutionContext(
        "parent",
        str(tmp_path),
        create_openai_client=lambda: {},
        session_manager=NS(get_resolved_settings=lambda: policy),
    )
    assert (await handle_subagent_tool({"description": "task", "prompt": "task"}, context)).ok
    assert seen == [policy]


def test_terminal_child_outputs_collapsed_and_bounded():
    panel = SubagentActivityBlock(max_agents=2, output_limit=8)
    handle = NS(id="child", description="Inspect code", parent_agent_id=None)
    panel.update("subagent/activity", {"event": TextPart("secret content")}, handle)
    panel.update("subagent/activity", {"event": ToolResultPart("call", "large output")}, handle)
    console = Console(record=True, width=80)
    console.print(panel.render())
    collapsed = console.export_text(clear=True)
    assert "Inspect code" in collapsed and "Ctrl-E" in collapsed
    assert "content" not in collapsed and "output" not in collapsed.replace("child output", "")
    console.print(panel.render(expanded=True))
    expanded = console.export_text()
    assert " content" in expanded and "e output" in expanded
    panel.update("subagent/end", {"stopReason": "aborted"}, handle)
    assert not panel.has_running


async def test_partial_child_stream_survives_interruption_on_disk(tmp_path, registry):
    started, release = threading.Event(), threading.Event()

    class Stream:
        def __iter__(self):
            yield NS(
                choices=[NS(delta=NS(content="partial handoff"), finish_reason=None)], usage=None
            )
            started.set()
            release.wait(timeout=2)
            yield NS(choices=[NS(delta=NS(content=" late text"), finish_reason=None)], usage=None)

        def close(self):
            pass

    spec = SubAgentSpec("partial", "work", parent_session_id="parent")
    manager = _manager(tmp_path, [Stream()])
    task = asyncio.create_task(manager.spawn_subagent(spec))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        manager.cancel_subagent(f"sub_parent_{spec.task_id}")
        result = await asyncio.wait_for(task, 0.5)
        assert result.status == "interrupted" and result.summary == "partial handoff"
        checkpoint = [
            json.loads(line)
            for line in spec.checkpoint_store.context_path(spec.task_id).read_text().splitlines()
        ]
        assert checkpoint[-1] == {"role": "assistant", "content": "partial handoff"}
        assert spec.checkpoint_store.output_path(spec.task_id).read_text() == "partial handoff"
        from coderai.wire.file import WireFile

        wire = WireFile(spec.checkpoint_store.instance_dir(spec.task_id) / "wire.jsonl")
        assert any(
            isinstance(event, TextPart) and event.text == "partial handoff"
            for event in wire.replay_sync()
        )
    finally:
        release.set()


async def test_child_compaction_keeps_task_and_complete_tool_group(tmp_path, monkeypatch, registry):
    received = []
    call = {"id": "call", "function": {"name": "read", "arguments": "{}"}}
    responses = iter(
        [
            _response(tool_calls=[call]),
            _response(tool_calls=[{**call, "id": "next"}]),
            _response("checkpoint: the old file has 100 rows"),
            _response("done"),
        ]
    )

    def create(**request):
        received.append(json.loads(json.dumps(request)))
        return next(responses)

    manager = SubAgentManager(
        str(tmp_path),
        lambda: {
            "client": NS(chat=NS(completions=NS(create=create))),
            "model": "test",
        },
        get_resolved_settings=lambda: {"compactionTriggerRatio": 0.5, "reservedContextSize": 1000},
    )
    manager._inherits_permission_settings = False
    monkeypatch.setattr(manager, "_get_sandboxed_tools", lambda *args: [])
    monkeypatch.setattr(
        "coderai.prompt.calculate_context_budget",
        lambda *args, **kwargs: {
            "context_limit": 20000,
            "max_output_tokens": 2000,
        },
    )
    monkeypatch.setattr(
        "coderai.tools.legacy.executor.ToolExecutor.execute_tool_calls",
        AsyncMock(
            return_value=[{"content": "x" * 24000}],
        ),
    )
    result = await manager.spawn_subagent(SubAgentSpec("task", "Preserve the initial objective"))
    assert result.status == "completed" and len(received) == 4
    summary_request = received[2]
    assert not summary_request.get("tools") and summary_request["tool_choice"] == "none"
    final_messages = received[-1]["messages"]
    assert "Preserve the initial objective" in final_messages[1]["content"]
    assert any("[Subagent context checkpoint]" in msg.get("content", "") for msg in final_messages)
    assert [msg["tool_call_id"] for msg in final_messages if msg["role"] == "tool"] == ["next"]


async def test_child_failed_compaction_preserves_original_messages():
    from coderai.subagents.compaction import compact_child_messages

    messages = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "old"},
        {"role": "assistant", "content": "latest"},
    ]
    original = json.loads(json.dumps(messages))
    assert (
        await compact_child_messages(
            messages,
            model="test",
            complete=AsyncMock(return_value=_response("bad", finish_reason="length")),
        )
        is None
    )
    assert messages == original


def test_shell_child_activity_updates_live_tree_and_pager(monkeypatch):
    from io import StringIO
    from coderai.ui.shell import app

    console = Console(file=StringIO(), record=True, force_terminal=True, width=80)
    monkeypatch.setattr(app, "console", console)
    monkeypatch.setattr(app, "_RICH", True)
    monkeypatch.setattr(console, "pager", lambda **kwargs: nullcontext())
    state = app._StreamState()
    handle = NS(id="child", description="Inspect code", parent_agent_id=None)
    state.on_subagent_event("subagent/start", {"id": "child"}, handle)
    assert state._subagent_live is not None
    state.on_subagent_event(
        "subagent/activity", {"event": TextPart("private child finding")}, handle
    )
    assert state.streamed_content == []
    state.on_subagent_event("subagent/end", {"stopReason": "completed"}, handle)
    assert state._subagent_live is None and state.has_expandable_panel()
    console.export_text(clear=True)
    assert state._show_expandable_panel_content()
    assert "private child finding" in console.export_text()


async def test_child_mcp_inherits_definitions_and_parent_mask(tmp_path, monkeypatch, registry):
    received = []
    blocked = "mcp__example__blocked"
    allowed = "mcp__example__allowed"
    call = {"id": "masked", "function": {"name": blocked, "arguments": "{}"}}
    responses = iter([_response(tool_calls=[call]), _response("done")])

    def create(**request):
        received.append(request)
        return next(responses)

    mcp = NS(
        is_mcp_tool=lambda name: name.startswith("mcp__"),
        is_tool_enabled_for_session=lambda session, name: name != blocked,
    )
    session_manager = NS(
        mcp_manager=mcp,
        get_external_tool_definitions=lambda: [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": "external",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for name in (blocked, allowed)
        ],
    )
    manager = SubAgentManager(
        str(tmp_path),
        lambda: {
            "client": NS(chat=NS(completions=NS(create=create))),
            "model": "test",
        },
    )
    executions = []

    class Executor:
        def __init__(self, *args, **kwargs):
            assert kwargs["mcp_manager"] is mcp

        async def execute_tool_calls(self, *args, **kwargs):
            executions.append(args)
            return []

    monkeypatch.setattr("coderai.tools.legacy.executor.ToolExecutor", Executor)
    result = await manager.spawn_subagent(
        SubAgentSpec(
            "external",
            "external",
            subagent_type="coder",
            parent_session_id="parent",
            session_manager=session_manager,
        )
    )
    assert result.status == "completed"
    names = [tool["function"]["name"] for tool in received[0]["tools"]]
    assert allowed in names and blocked not in names
    assert executions == []
    messages = registry.get(result.task_id).conversation
    assert "disabled in the parent session" in next(
        msg["content"] for msg in messages if msg["role"] == "tool"
    )


def test_descriptor_policy_uses_aliases_and_external_namespace_patterns():
    restriction = ToolRestriction(deny=["Read"])
    assert not restriction.is_tool_permitted("read_file")
    restriction = ToolRestriction(allow=["mcp__example__*"])
    assert restriction.is_tool_permitted("mcp__example__lookup")
    assert not restriction.is_tool_permitted("mcp__other__lookup")


def test_continuable_settlement_stops_spinner_before_worker_exit():
    panel = SubagentActivityBlock()
    handle = NS(id="child", description="Inspect code", parent_agent_id=None)
    panel.update("subagent/start", {"id": "child"}, handle)
    panel.update(
        "subagent/activity",
        {
            "event": SubagentEvent(
                agent_id="child",
                event={"type": "subagent.settled", "status": "completed", "summary": "done"},
            )
        },
        handle,
    )
    assert not panel.has_running and panel.activities["child"].text == "done"


async def test_child_request_honors_configured_window_and_zero_reserve(tmp_path, monkeypatch):
    received = []

    def create(**request):
        received.append(request)
        return _response("done")

    manager = SubAgentManager(
        str(tmp_path),
        lambda: {"client": NS(chat=NS(completions=NS(create=create))), "model": "gpt-4o"},
        get_resolved_settings=lambda: {"contextWindow": 8000, "reservedContextSize": 0},
    )
    monkeypatch.setattr(manager, "_get_sandboxed_tools", lambda *args: [])
    result = await manager.spawn_subagent(SubAgentSpec("small window", "finish"))
    assert result.status == "completed"
    assert 0 < received[-1]["max_tokens"] <= 800


def test_parent_fork_keeps_tool_pairing_and_omits_inflight_group():
    from coderai.tools.agent import _extract_seed_messages

    def call(id):
        return {"id": id, "type": "function", "function": {"name": "read", "arguments": "{}"}}

    rows = [
        {"role": "user", "content": "objective"},
        {"role": "assistant", "content": "", "tool_calls": [call("finished")]},
        {"role": "tool", "content": "result", "tool_call_id": "finished"},
        {"role": "assistant", "content": "", "tool_calls": [call("pending")]},
    ]
    context = ToolExecutionContext(
        session_id="parent", project_root="/tmp", list_session_messages=lambda sid: rows
    )
    snapshot = _extract_seed_messages(context)
    assert snapshot == rows[:3]
    snapshot[1]["tool_calls"][0]["id"] = "child"
    assert rows[1]["tool_calls"][0]["id"] == "finished"
