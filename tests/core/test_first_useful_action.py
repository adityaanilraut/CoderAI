"""Time-to-first-useful-action semantics and telemetry privacy."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coderAI.core.tool_executor import BatchStatus, ToolExecutor
from coderAI.core.turn import TurnContext
from coderAI.system.history import Session


def test_first_successful_relevant_action_records_elapsed_time_once() -> None:
    turn = TurnContext(objective_started_at=10.0, routed_tool_names={"read_file", "grep"})

    assert (
        turn.record_first_useful_action(
            "read_file", {"success": False, "error": "nope"}, executed=True, now=10.1
        )
        is None
    )
    payload = turn.record_first_useful_action(
        "grep", {"success": True, "content": "secret result"}, executed=True, now=10.125
    )
    assert payload == {"tool_name": "grep", "elapsed_ms": 125}
    assert (
        turn.record_first_useful_action("read_file", {"success": True}, executed=True, now=11.0)
        is None
    )


def test_control_failed_irrelevant_cached_and_synthetic_activity_is_not_useful() -> None:
    turn = TurnContext(
        objective_started_at=1.0,
        routed_tool_names={
            "manage_tasks",
            "submit_plan",
            "request_plan_amendment",
            "use_skill",
            "read_file",
        },
    )

    for name in ("manage_tasks", "submit_plan", "request_plan_amendment", "use_skill"):
        assert (
            turn.record_first_useful_action(name, {"success": True}, executed=True, now=1.1) is None
        )
    assert (
        turn.record_first_useful_action("write_file", {"success": True}, executed=True, now=1.2)
        is None
    )
    assert (
        turn.record_first_useful_action("read_file", {"success": True}, executed=False, now=1.3)
        is None
    )
    assert (
        turn.record_first_useful_action(
            "internal_recovery", {"success": True}, executed=False, now=1.4
        )
        is None
    )
    assert turn.first_useful_action_elapsed_ms is None


def test_event_payload_cannot_expose_objective_arguments_or_result_content() -> None:
    turn = TurnContext(
        user_message="read password=super-secret",
        objective_started_at=5.0,
        routed_tool_names={"read_file"},
    )
    payload = turn.record_first_useful_action(
        "read_file",
        {"success": True, "content": "private file body"},
        executed=True,
        now=5.25,
    )

    assert payload == {"tool_name": "read_file", "elapsed_ms": 250}
    serialized = str(payload)
    assert "password" not in serialized
    assert "super-secret" not in serialized
    assert "private file body" not in serialized
    assert set(payload or {}) == {"tool_name", "elapsed_ms"}


def test_new_turn_resets_useful_action_clock_and_observation() -> None:
    first = TurnContext(objective_started_at=1.0, routed_tool_names={"read_file"})
    first.record_first_useful_action("read_file", {"success": True}, executed=True, now=1.5)
    second = TurnContext(objective_started_at=10.0, routed_tool_names={"read_file"})

    assert first.first_useful_action_elapsed_ms == 500
    assert second.first_useful_action_elapsed_ms is None
    assert second.record_first_useful_action(
        "read_file", {"success": True}, executed=True, now=10.05
    ) == {"tool_name": "read_file", "elapsed_ms": 50}


@pytest.mark.asyncio
async def test_executor_emits_first_useful_action_after_real_success_only() -> None:
    tool = SimpleNamespace(
        requires_confirmation=False,
        is_read_only=True,
        max_parallel_invocations=0,
        result_provenance="trusted",
    )
    registry = SimpleNamespace(
        get=MagicMock(return_value=tool),
        execute=AsyncMock(return_value={"success": True, "content": "private body"}),
    )
    tool_calls = [
        {
            "id": "t1",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"secret.txt"}'},
        }
    ]
    session = Session(session_id="session_1234567890_deadbeef")
    session.add_message("assistant", None, tool_calls=tool_calls)
    events = MagicMock()
    agent = SimpleNamespace(
        auto_approve=True,
        approval_port=None,
        tools=registry,
        tracker_info=None,
        session=session,
        context_controller=SimpleNamespace(summarize_tool_result=lambda result: result),
        provider=SimpleNamespace(get_model_info=lambda: {"total_tokens": 0}),
        _sync_tracker=MagicMock(),
        _finish_tracker=MagicMock(),
        save_session=MagicMock(),
        _workspace_trusted=False,
        config=SimpleNamespace(),
    )
    services = SimpleNamespace(events=events, config=agent.config)
    turn = TurnContext(objective_started_at=10.0, routed_tool_names={"read_file"})
    executor = ToolExecutor(agent)

    with (
        patch("coderAI.core.tool_executor.get_services", return_value=services),
        patch("coderAI.core.turn.time.monotonic", return_value=10.125),
    ):
        outcome = await executor.orchestrate_tool_calls(
            tool_calls=tool_calls,
            messages=session.get_messages_for_api(),
            user_message="read password=super-secret",
            hooks_data=None,
            hooks_manager=SimpleNamespace(run_hooks=AsyncMock(return_value=[])),
            turn=turn,
        )

    assert outcome.status is BatchStatus.OK
    event = next(
        call for call in events.emit.call_args_list if call.args == ("first_useful_action",)
    )
    assert event.kwargs == {"tool_name": "read_file", "elapsed_ms": 125}
    assert "super-secret" not in str(event)
    assert "secret.txt" not in str(event)
    assert "private body" not in str(event)
