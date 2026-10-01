"""Dispatch boundaries retain chunk, persistence, callback and abort ordering."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from coderai.soul.session.approval import unregister_session_manager
from coderai.soul.session.manager import SessionManager
from coderai.tools.legacy.types import TOOL_ABORTED_BEFORE_DISPATCH


def _call(call_id, name, args=None):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args or {})},
    }


async def test_chunk_and_result_order_survives_barrier_stop(tmp_path):
    trace = []
    manager = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": None},
        get_resolved_settings=lambda: {
            "model": "test",
            "permissions": {"sandbox": "workspace-write"},
        },
        on_assistant_message=lambda message, tool: trace.append(("notify", message.tool_call_id)),
    )
    session = await manager.create_empty_session()
    original = manager._append_message

    def append(message):
        trace.append(("append", message.role, message.tool_call_id, message.content))
        original(message)

    manager._append_message = append
    manager.file_history = NS(
        record_checkpoint=lambda sid, paths, label: trace.append(("checkpoint", label))
    )

    class Executor:
        registry = NS(
            get=lambda name: NS(
                check_execution_mode=lambda args: {
                    "read": "parallel",
                    "edit": "sequential",
                    "exit_plan_mode": "barrier",
                }[name]
            )
        )

        async def execute_tool_calls(self, sid, calls, hooks, parallel):
            trace.append(("execute", [call["id"] for call in calls], parallel))
            results = []
            for call in calls:
                result = {"metadata": {}, "followUpMessages": []}
                if call["id"] == "r1":
                    result["followUpMessages"] = [
                        {
                            "role": "system",
                            "content": "advisory",
                            "contentParams": [{"type": "text", "text": "detail"}],
                        }
                    ]
                if call["id"] == "b1":
                    result.update(
                        awaitUserResponse=True, forceStopTurn=True, metadata={"hookStop": True}
                    )
                results.append({"toolCallId": call["id"], "content": call["id"], "result": result})
            return results

    manager.tool_executor = Executor()
    calls = [
        _call("r1", "read"),
        _call("r2", "read"),
        _call("w1", "edit", {"file_path": "a.txt"}),
        _call("b1", "exit_plan_mode"),
        _call("r3", "read"),
    ]
    try:
        outcome = await manager._append_tool_messages(session, calls)
        assert outcome.waiting and outcome.stop_reason == "hook"
        assert [item for item in trace if item[0] == "execute"] == [
            ("execute", ["r1", "r2"], True),
            ("execute", ["w1"], False),
            ("execute", ["b1"], False),
        ]
        rows = manager.list_session_messages(session)
        tools = [message for message in rows if message.role == "tool"]
        assert [message.tool_call_id for message in tools] == ["r1", "r2", "w1", "b1", "r3"]
        assert TOOL_ABORTED_BEFORE_DISPATCH in tools[-1].content
        advisory = next(message for message in rows if message.content == "advisory")
        assert advisory.role == "user" and advisory.meta["advisoryRole"] == "system"
        assert advisory.meta["contentParams"] == [{"type": "text", "text": "detail"}]
        assert trace.index(("notify", "r1")) < next(
            i for i, item in enumerate(trace) if item[:3] == ("append", "user", None)
        )
        assert [(item[0], item[1]) for item in trace if item[0] == "checkpoint"] == [
            ("checkpoint", "After tool execution")
        ]
    finally:
        manager.close_event_streams()
        unregister_session_manager(manager)


@pytest.mark.parametrize("cancel", [False, True])
async def test_dispatch_failure_or_cancellation_pairs_remaining_calls(tmp_path, cancel):
    manager = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": None},
        get_resolved_settings=lambda: {"model": "test"},
    )
    session = await manager.create_empty_session()

    async def fail(*args, **kwargs):
        if cancel:
            raise asyncio.CancelledError()
        raise RuntimeError("executor failed")

    manager.tool_executor = NS(registry=None, execute_tool_calls=fail)
    try:
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
            await manager._append_tool_messages(
                session, [_call("first", "read"), _call("second", "edit")]
            )
        tools = [
            message for message in manager.list_session_messages(session) if message.role == "tool"
        ]
        assert [message.tool_call_id for message in tools] == ["first", "second"]
        assert all(TOOL_ABORTED_BEFORE_DISPATCH in message.content for message in tools)
    finally:
        manager.close_event_streams()
        unregister_session_manager(manager)
