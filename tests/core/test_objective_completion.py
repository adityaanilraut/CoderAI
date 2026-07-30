"""Completion-gate coverage for structured objective evidence."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coderAI.core.agent_loop import ExecutionLoop
from coderAI.core.objective import ObjectiveState
from coderAI.core.tool_executor import BatchStatus, ToolBatchOutcome


def _tool_call(call_id: str, name: str, arguments: str) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _tool(*, category: str, read_only: bool) -> SimpleNamespace:
    return SimpleNamespace(category=category, is_read_only=read_only)


def test_workspace_change_requires_post_mutation_inspection_and_check():
    state = ObjectiveState("Fix the parser")
    state.record_tool_result(
        "write_file",
        {"path": "parser.py"},
        {"success": True, "message": "updated"},
        _tool(category="filesystem", read_only=False),
    )

    decision = state.evaluate_completion()

    assert decision.allowed is False
    assert any("verification" in issue for issue in decision.issues)
    assert any("not inspected" in issue for issue in decision.issues)


def test_workspace_change_becomes_verified_only_with_fresh_evidence():
    state = ObjectiveState("Fix the parser")
    state.record_tool_result(
        "write_file",
        {"path": "parser.py"},
        {"success": True},
        _tool(category="filesystem", read_only=False),
    )
    state.record_tool_result(
        "read_file",
        {"path": "parser.py"},
        {"success": True, "content": "fixed"},
        _tool(category="filesystem", read_only=True),
    )
    state.record_tool_result(
        "run_tests",
        {"path": "tests/test_parser.py"},
        {"success": True, "message": "3 passed"},
        _tool(category="code_quality", read_only=False),
    )

    decision = state.evaluate_completion()

    assert decision.allowed is True
    assert decision.status == "verified"
    assert state.as_dict()["completion_status"] == "verified"


def test_latest_failed_tool_outcome_prevents_false_success():
    state = ObjectiveState("Fix the parser")
    state.record_tool_result(
        "write_file",
        {"path": "parser.py"},
        {"success": True},
        _tool(category="filesystem", read_only=False),
    )
    state.record_tool_result(
        "read_file",
        {"path": "parser.py"},
        {"success": True},
        _tool(category="filesystem", read_only=True),
    )
    state.record_tool_result(
        "run_tests",
        {"path": "tests/test_parser.py"},
        {"success": False, "error": "1 failed"},
        _tool(category="code_quality", read_only=False),
    )

    decision = state.evaluate_completion()

    assert decision.allowed is False
    assert any("run_tests" in issue for issue in decision.issues)


def test_failed_tool_does_not_become_reasoned_success_without_a_mutation():
    state = ObjectiveState("Read the configuration")
    state.record_tool_result(
        "read_file",
        {"path": "missing.toml"},
        {"success": False, "error": "not found"},
        _tool(category="filesystem", read_only=True),
    )

    decision = state.evaluate_completion()

    assert decision.allowed is False
    assert decision.status == "incomplete"


@pytest.mark.asyncio
async def test_loop_rejects_premature_completion_then_accepts_verified_work(mock_agent):
    loop = ExecutionLoop(mock_agent)
    mock_agent.config.completion_gate_enabled = True
    mock_agent.config.completion_gate_max_retries = 1
    responses = [
        {
            "content": None,
            "tool_calls": [_tool_call("edit", "write_file", '{"path":"parser.py"}')],
            "finish_reason": "tool_calls",
        },
        {"content": "Done.", "tool_calls": None, "finish_reason": "stop"},
        {
            "content": None,
            "tool_calls": [
                _tool_call("inspect", "read_file", '{"path":"parser.py"}'),
                _tool_call("test", "run_tests", '{"path":"tests/test_parser.py"}'),
            ],
            "finish_reason": "tool_calls",
        },
        {"content": "Fixed and verified.", "tool_calls": None, "finish_reason": "stop"},
    ]
    loop._call_llm_with_retry = AsyncMock(side_effect=responses)

    async def record_batch(tool_calls, *_args, turn=None, **_kwargs):
        assert turn is not None
        for call in tool_calls:
            name = call["function"]["name"]
            arguments = __import__("json").loads(call["function"]["arguments"])
            tool = _tool(
                category="code_quality" if name == "run_tests" else "filesystem",
                read_only=name == "read_file",
            )
            turn.objective_state.record_tool_result(
                name, arguments, {"success": True, "message": "ok"}, tool
            )
        return ToolBatchOutcome(BatchStatus.OK)

    loop.tool_executor.orchestrate_tool_calls = AsyncMock(side_effect=record_batch)

    result = await loop.run("Fix the parser")

    assert result["success"] is True
    assert result["objective_state"]["completion_status"] == "verified"
    assert result["objective_state"]["artifacts_changed"] == ["parser.py"]
    assert loop._call_llm_with_retry.await_count == 4
    assert any(
        message.role == "system" and "[Completion Gate]" in (message.content or "")
        for message in mock_agent.session.messages
    )


@pytest.mark.asyncio
async def test_loop_reports_unverified_instead_of_success(mock_agent):
    loop = ExecutionLoop(mock_agent)
    mock_agent.config.completion_gate_enabled = True
    mock_agent.config.completion_gate_max_retries = 0
    responses = [
        {
            "content": None,
            "tool_calls": [_tool_call("edit", "write_file", '{"path":"parser.py"}')],
            "finish_reason": "tool_calls",
        },
        {"content": "Done.", "tool_calls": None, "finish_reason": "stop"},
    ]
    loop._call_llm_with_retry = AsyncMock(side_effect=responses)

    async def record_mutation(tool_calls, *_args, turn=None, **_kwargs):
        turn.objective_state.record_tool_result(
            "write_file",
            {"path": "parser.py"},
            {"success": True},
            _tool(category="filesystem", read_only=False),
        )
        return ToolBatchOutcome(BatchStatus.OK)

    loop.tool_executor.orchestrate_tool_calls = AsyncMock(side_effect=record_mutation)

    result = await loop.run("Fix the parser")

    assert result["success"] is False
    assert result["stop_reason"] == "unverified"
    assert result["objective_state"]["completion_status"] == "unverified"


@pytest.mark.asyncio
async def test_execution_result_exposes_approved_plan_link(mock_agent):
    loop = ExecutionLoop(mock_agent)
    mock_agent.active_plan_id = "plan-123"
    mock_agent.active_plan_revision = 4
    loop._call_llm_with_retry = AsyncMock(
        return_value={"content": "Plan executed.", "tool_calls": None, "finish_reason": "stop"}
    )

    result = await loop.run("Execute the approved plan")

    assert result["objective_state"]["plan_id"] == "plan-123"
    assert result["objective_state"]["plan_revision"] == 4
