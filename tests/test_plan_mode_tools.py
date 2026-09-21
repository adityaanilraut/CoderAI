import pytest
from coderai.tools.legacy.types import ToolExecutionContext, ToolExecutionHooks
from coderai.tools.plan import handle_exit_plan_mode_tool, handle_enter_plan_mode_tool
from coderai.tools.legacy.executor import ToolExecutor
from coderai.tools.legacy.registry import get_tool_registry


class DummySessionManager:
    def __init__(self, plan_mode: bool = False):
        self._entries = {"s1": {"planMode": plan_mode}}

    def _get_entry(self, session_id: str):
        return self._entries.get(session_id, {})


def test_exit_plan_mode_when_in_plan_mode():
    ctx = ToolExecutionContext(
        session_id="s1",
        project_root="/tmp",
        plan_mode=True,
    )
    res = handle_exit_plan_mode_tool({"plan": "# Implementation Plan\nStep 1: Code"}, ctx)
    assert res.ok is True
    assert res.name == "exit_plan_mode"
    assert res.metadata.get("exitPlanMode") is True
    assert res.concludes_turn is True
    assert "Implementation Plan" in res.output


def test_exit_plan_mode_empty_args_in_plan_mode():
    ctx = ToolExecutionContext(
        session_id="s1",
        project_root="/tmp",
        plan_mode=True,
    )
    res = handle_exit_plan_mode_tool({}, ctx)
    assert res.ok is True
    assert res.metadata.get("exitPlanMode") is True
    assert res.concludes_turn is True
    assert res.output == "Plan completed."


def test_exit_plan_mode_summary_arg():
    ctx = ToolExecutionContext(
        session_id="s1",
        project_root="/tmp",
        plan_mode=True,
    )
    res = handle_exit_plan_mode_tool(
        {"summary": "Finished exploring and wrote verification test"}, ctx
    )
    assert res.ok is True
    assert res.metadata.get("exitPlanMode") is True
    assert res.concludes_turn is True
    assert res.output == "Finished exploring and wrote verification test"


def test_exit_plan_mode_idempotent_when_not_in_plan_mode():
    ctx = ToolExecutionContext(
        session_id="s1",
        project_root="/tmp",
        plan_mode=False,
    )
    res = handle_exit_plan_mode_tool({}, ctx)
    assert res.ok is True
    assert res.metadata.get("exitPlanMode") is True
    assert res.concludes_turn is True
    assert "Plan mode is not active" in res.output


def test_exit_plan_mode_detects_plan_mode_from_session_manager():
    mgr = DummySessionManager(plan_mode=True)
    ctx = ToolExecutionContext(
        session_id="s1",
        project_root="/tmp",
        plan_mode=False,
        session_manager=mgr,
    )
    res = handle_exit_plan_mode_tool({"summary": "Ready to execute"}, ctx)
    assert res.ok is True
    assert res.metadata.get("exitPlanMode") is True
    assert res.concludes_turn is True
    assert res.output == "Ready to execute"


def test_exit_plan_mode_invalid_plan_heading():
    ctx = ToolExecutionContext(
        session_id="s1",
        project_root="/tmp",
        plan_mode=True,
    )
    res = handle_exit_plan_mode_tool({"plan": "No heading here"}, ctx)
    assert res.ok is False
    assert "requires a non-empty markdown plan starting with a # heading" in res.error


def test_enter_plan_mode_when_inactive():
    ctx = ToolExecutionContext(
        session_id="s1",
        project_root="/tmp",
        plan_mode=False,
    )
    res = handle_enter_plan_mode_tool({}, ctx)
    assert res.ok is True
    assert res.metadata.get("enterPlanMode") is True
    assert "Plan mode activated" in res.output


def test_enter_plan_mode_idempotent_when_already_active():
    ctx = ToolExecutionContext(
        session_id="s1",
        project_root="/tmp",
        plan_mode=True,
    )
    res = handle_enter_plan_mode_tool({}, ctx)
    assert res.ok is True
    assert res.metadata.get("enterPlanMode") is True
    assert "Already in plan mode" in res.output


def test_enter_plan_mode_requires_session():
    ctx = ToolExecutionContext(
        session_id="",
        project_root="/tmp",
    )
    res = handle_enter_plan_mode_tool({}, ctx)
    assert res.ok is False
    assert "requires an active session" in res.error


@pytest.mark.asyncio
async def test_tool_executor_propagates_plan_mode_and_exits_cleanly():
    executor = ToolExecutor(project_root="/tmp", registry=get_tool_registry())
    hooks = ToolExecutionHooks(plan_mode=True)
    tool_call = {
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "exit_plan_mode",
            "arguments": '{"summary": "All steps planned"}',
        },
    }
    executions = await executor.execute_tool_calls("s1", [tool_call], hooks=hooks)
    assert len(executions) == 1
    res = executions[0]["result"]
    assert res.get("ok") is True
    assert res.get("name") == "exit_plan_mode"
    assert res.get("metadata", {}).get("exitPlanMode") is True
