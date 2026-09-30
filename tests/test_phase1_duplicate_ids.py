"""Tool-call identities remain unique before grants and batch dispatch."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coderai.soul.approval import compute_tool_call_permissions
from coderai.hooks import MergedHookOutcome
from coderai.soul.session.completion import normalize_tool_calls
from coderai.tools.legacy.executor import ToolExecutor
from coderai.tools.legacy.registry import ToolRegistry
from coderai.tools.legacy.schema import define_tool
from coderai.tools.legacy.types import (
    ToolCallAuthorization,
    ToolExecutionHooks,
    ToolResult,
    tool_arguments_digest,
)


def _call(call_id, path="one.txt"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "write", "arguments": json.dumps({"file_path": path, "content": "x"})},
    }


def test_model_duplicate_ids_get_separate_permission_requests(tmp_path):
    original = [_call("repeated"), _call("repeated", "two.txt")]
    calls = normalize_tool_calls(original)
    assert calls[0]["id"] == "repeated"
    assert calls[1]["id"] != "repeated"
    assert original[1]["id"] == "repeated"
    assert calls[1]["function"] == original[1]["function"]
    plan = compute_tool_call_permissions(
        session_id="duplicates", project_root=str(tmp_path), tool_calls=calls
    )
    assert len(plan["askPermissions"]) == 2
    assert len({request["toolCallId"] for request in plan["askPermissions"]}) == 2


def test_mixed_object_calls_and_missing_ids_are_unique():
    object_call = SimpleNamespace(id="same", function=SimpleNamespace(name="write", arguments="{}"))
    calls = normalize_tool_calls([_call("same"), object_call, _call(None), _call(9)])
    ids = [call["id"] for call in calls]
    assert len(set(ids)) == 4
    assert all(isinstance(call_id, str) and call_id for call_id in ids)


def test_generated_ids_do_not_collide_with_later_original_ids(monkeypatch):
    generated = iter(["later", "new-id"])
    monkeypatch.setattr(
        "coderai.soul.session.completion.uuid.uuid4",
        lambda: SimpleNamespace(hex=next(generated)),
    )
    calls = normalize_tool_calls([_call("same"), _call("same"), _call("later")])
    assert [call["id"] for call in calls] == ["same", "new-id", "later"]


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel", [False, True])
async def test_direct_duplicate_batch_rejects_all_calls_before_dispatch(
    tmp_path, monkeypatch, parallel
):
    executor = ToolExecutor(project_root=str(tmp_path))
    handler = AsyncMock(return_value=ToolResult(ok=True, name="write", output="unexpected"))
    monkeypatch.setattr(executor, "execute_tool_call", handler)
    calls = [_call("unique"), _call("repeated"), _call("repeated", "two.txt")]
    results = await executor.execute_tool_calls("duplicates", calls, parallel=parallel)
    handler.assert_not_awaited()
    assert len(results) == len(calls)
    assert all(not result["result"]["ok"] for result in results)
    assert all("DuplicateToolCallId" in result["result"]["error"] for result in results)


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel", [False, True])
async def test_unique_batch_keeps_normal_dispatch(tmp_path, monkeypatch, parallel):
    executor = ToolExecutor(project_root=str(tmp_path))
    handler = AsyncMock(return_value=ToolResult(ok=True, name="write", output="done"))
    monkeypatch.setattr(executor, "execute_tool_call", handler)
    calls = [_call("first"), _call("second")]
    results = await executor.execute_tool_calls("duplicates", calls, parallel=parallel)
    assert handler.await_count == 2
    assert [result["toolCallId"] for result in results] == ["first", "second"]
    assert all(result["result"]["ok"] for result in results)


@pytest.mark.asyncio
@pytest.mark.parametrize("hook_key", ["PreToolUse", "preToolUse", "pre_tool_call"])
async def test_direct_parallel_batch_halt_prevents_sibling_handlers(
    tmp_path, monkeypatch, hook_key
):
    monkeypatch.setattr(
        "coderai.hooks.load_hook_config", lambda *_args: {hook_key: [{"command": "fake"}]}
    )
    seen_hooks = []

    def pre_hook(_point, payload, **_kwargs):
        seen_hooks.append(payload["tool_input"]["file_path"])
        return MergedHookOutcome(stop=True, stop_reason="halt")

    monkeypatch.setattr("coderai.hooks.run_hook_point", pre_hook)
    executed = []

    async def writer(args, _context):
        executed.append(args["file_path"])
        return "unexpected"

    registry = ToolRegistry()
    registry.register(
        define_tool(
            name="write",
            description="Test writer",
            parameters={"file_path": {"type": "string"}, "content": {"type": "string"}},
            required=["file_path", "content"],
            handler=writer,
        )
    )
    executor = ToolExecutor(project_root=str(tmp_path), registry=registry)
    result = await executor.execute_tool_calls(
        "halt", [_call("first"), _call("second", "two.txt")], parallel=True
    )
    assert not executed
    assert seen_hooks == ["one.txt"]
    assert len(result) == 1
    assert result[0]["result"]["forceStopTurn"]


@pytest.mark.asyncio
async def test_no_pre_tool_hooks_preserves_parallel_execution(tmp_path, monkeypatch):
    monkeypatch.setattr("coderai.hooks.load_hook_config", lambda *_args: {})
    executor = ToolExecutor(project_root=str(tmp_path))
    second_started = asyncio.Event()

    async def execute(_session, call, _hooks):
        if call["id"] == "first":
            await second_started.wait()
        else:
            second_started.set()
        return ToolResult(ok=True, name="write", output="done")

    monkeypatch.setattr(executor, "execute_tool_call", execute)
    result = await asyncio.wait_for(
        executor.execute_tool_calls("parallel", [_call("first"), _call("second")], parallel=True),
        1,
    )
    assert len(result) == 2 and all(item["result"]["ok"] for item in result)


@pytest.mark.asyncio
async def test_direct_parallel_cached_halt_prevents_sibling_handlers(tmp_path, monkeypatch):
    monkeypatch.setattr("coderai.hooks.load_hook_config", lambda *_args: {})
    monkeypatch.setattr("coderai.hooks.run_hook_point", lambda *_a, **_k: MergedHookOutcome())
    executor = ToolExecutor(project_root=str(tmp_path))
    handler = AsyncMock(return_value=ToolResult(ok=True, name="write", output="unexpected"))
    monkeypatch.setattr(executor, "_run_handler", handler)
    first = _call("first")
    hooks = ToolExecutionHooks(
        authorizations={
            "first": ToolCallAuthorization(
                session_id="halt",
                tool_call_id="first",
                tool_name="write",
                project_root=str(tmp_path.resolve()),
                args_digest=tool_arguments_digest(json.loads(first["function"]["arguments"])),
                hook_outcome={"stop": True},
            )
        }
    )
    result = await executor.execute_tool_calls(
        "halt", [first, _call("second", "two.txt")], hooks=hooks, parallel=True
    )
    handler.assert_not_awaited()
    assert len(result) == 1 and result[0]["result"]["forceStopTurn"]


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
async def test_hook_input_change_requires_replan(tmp_path, monkeypatch, changed):
    call = _call("updated")
    original_args = json.loads(call["function"]["arguments"])
    updated_args = {**original_args, "content": "changed"} if changed else original_args.copy()
    monkeypatch.setattr(
        "coderai.hooks.run_hook_point",
        lambda *_args, **_kwargs: MergedHookOutcome(
            updated_input=updated_args, additional_context=["trusted hook context"]
        ),
    )
    executor = ToolExecutor(project_root=str(tmp_path))
    handler = AsyncMock(return_value=ToolResult(ok=True, name="write", output="done"))
    monkeypatch.setattr(executor, "_run_handler", handler)
    result = await executor.execute_tool_call("updated", call)
    if changed:
        handler.assert_not_awaited()
        assert not result.ok and "PreToolUseInputChangeRequiresReplan" in result.error
    else:
        handler.assert_awaited_once()
        assert result.ok
        assert handler.await_args.args[1] == original_args
        assert any(
            message.content == "trusted hook context" for message in result.follow_up_messages
        )
