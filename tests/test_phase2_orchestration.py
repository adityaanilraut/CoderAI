"""Comprehensive tests for Phase 2: Advanced Multi-Agent Orchestration.

Covers:
1. Workflow Scripting Engine (agent, pipeline, parallel, phase, log, schema validation, tool handler).
2. Ralph Automated Verification Engine (handoff protocol, rounds, context accumulation, verdict).
3. Agent Teams Coordination Seam & Swarm Tools (teammates, task board, mailboxes, wait_agent).
4. Tool Registry & Permissions integration.
"""

import asyncio
import json
import tempfile
import pytest

from coderai.core.permissions import describe_tool_permission_request
from coderai.core.teams import (
    TeamTaskBoard,
    get_team_manager,
    handle_spawn_teammate_tool,
    handle_team_task_create_tool,
    handle_team_task_get_tool,
    handle_team_task_list_tool,
    handle_team_task_update_tool,
    handle_wait_agent_tool,
    reset_team_manager,
)
from coderai.core.tools.ralph import (
    _parse_handoff,
    handle_ralph_tool,
)
from coderai.core.tools.registry import get_tool_registry
from coderai.core.tools.types import ToolExecutionContext
from coderai.core.workflow import (
    WorkflowContext,
    WorkflowEngine,
    execute_workflow_script,
    handle_workflow_tool,
)


@pytest.fixture
def temp_workspace():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def mock_client_factory():
    """Mock client factory for testing LLM calls."""

    def _factory():
        class MockCompletions:
            def create(self, **kwargs):
                messages = kwargs.get("messages", [])
                user_msg = messages[-1].get("content", "") if messages else ""

                # Check if this is a Ralph verification request
                if "Ralph Automated Verification" in user_msg:
                    if "Round 1/" in user_msg:
                        content = json.dumps(
                            {
                                "status": "continue",
                                "summary": "Round 1 completed initial code analysis.",
                                "evidence": "Found 3 files.",
                                "next_steps": "Run pytest to verify unit tests.",
                            }
                        )
                    else:
                        content = json.dumps(
                            {
                                "status": "complete",
                                "summary": "All tests pass and objective verified.",
                                "evidence": "pytest returned exit code 0.",
                                "next_steps": "",
                            }
                        )
                elif "JSON Schema" in user_msg:
                    content = json.dumps(
                        {
                            "architecture": "microservices",
                            "components": ["auth", "billing", "gateway"],
                            "valid": True,
                        }
                    )
                else:
                    content = "Task finished successfully."

                msg = type(
                    "M",
                    (),
                    {
                        "content": content,
                        "tool_calls": None,
                        "reasoning_content": None,
                        "refusal": None,
                    },
                )()
                usage = type(
                    "U", (), {"prompt_tokens": 20, "completion_tokens": 15, "total_tokens": 35}
                )()
                return type(
                    "R", (), {"choices": [type("C", (), {"message": msg})()], "usage": usage}
                )()

        class MockChat:
            completions = MockCompletions()

        class MockClient:
            chat = MockChat()

        return {
            "client": MockClient(),
            "model": "gpt-5.6-luna",
            "baseURL": None,
            "temperature": 0.0,
            "thinkingEnabled": False,
            "reasoningEffort": "high",
        }

    return _factory


@pytest.fixture
def tool_context(temp_workspace, mock_client_factory):
    return ToolExecutionContext(
        session_id="test_phase2_session",
        project_root=temp_workspace,
        create_openai_client=mock_client_factory,
    )


# =====================================================================
# 1. Workflow Scripting Engine Tests
# =====================================================================


@pytest.mark.asyncio
async def test_workflow_engine_primitives(temp_workspace, mock_client_factory):
    wf_ctx = WorkflowContext(
        workflow_id="wf_test_1",
        name="Test Workflow",
        project_root=temp_workspace,
        create_openai_client=mock_client_factory,
    )
    engine = WorkflowEngine(wf_ctx)

    # 1. Test phase and log
    wf_ctx.phase("Phase 1: Exploration")
    wf_ctx.log("Exploration started")
    assert len(wf_ctx.phases) == 1
    assert wf_ctx.phases[0].title == "Phase 1: Exploration"
    assert len(wf_ctx.logs) == 2  # 1 from phase, 1 from log

    # 2. Test parallel primitive
    async def task_a():
        await asyncio.sleep(0.01)
        return "result_a"

    def task_b():
        return "result_b"

    results = await engine.parallel([task_a, task_b], max_concurrency=2)
    assert results == ["result_a", "result_b"]

    # 3. Test pipeline primitive (streaming through stages)
    async def stage_1(item: int) -> int:
        return item * 2

    def stage_2(item: int) -> str:
        return f"item_{item}"

    pipeline_res = await engine.pipeline([1, 2, 3], stage_1, stage_2)
    assert pipeline_res == ["item_2", "item_4", "item_6"]

    # 4. Test agent primitive with schema validation
    schema = {
        "type": "object",
        "properties": {
            "architecture": {"type": "string"},
            "components": {"type": "array"},
            "valid": {"type": "boolean"},
        },
        "required": ["architecture", "components"],
    }
    agent_res = await engine.agent("Analyze system architecture", opts={"schema": schema})
    assert agent_res["status"] == "completed"
    assert agent_res["data"] is not None
    assert agent_res["data"]["architecture"] == "microservices"
    assert "billing" in agent_res["data"]["components"]


@pytest.mark.asyncio
async def test_execute_workflow_script(temp_workspace, mock_client_factory):
    wf_ctx = WorkflowContext(
        workflow_id="wf_test_script",
        name="Multi-Phase Script",
        project_root=temp_workspace,
        create_openai_client=mock_client_factory,
    )

    script = """
async def main(args):
    phase("Phase 1: Discovery")
    log("Running discovery")

    async def inspect_module(name):
        return f"module_{name}_ok"

    results = await parallel([
        lambda: inspect_module("auth"),
        lambda: inspect_module("core"),
    ])

    phase("Phase 2: Aggregation")
    pipeline_out = await pipeline([10, 20], lambda x: x + 1, lambda x: f"val_{x}")

    return {
        "status": "success",
        "modules": results,
        "pipeline": pipeline_out,
        "input_arg": args.get("target"),
    }
"""

    res = await execute_workflow_script(script, {"target": "production"}, wf_ctx)
    assert res.status == "completed"
    assert len(res.phases) == 2
    assert res.phases[0].title == "Phase 1: Discovery"
    assert res.phases[1].title == "Phase 2: Aggregation"
    assert res.output["status"] == "success"
    assert res.output["modules"] == ["module_auth_ok", "module_core_ok"]
    assert res.output["pipeline"] == ["val_11", "val_21"]
    assert res.output["input_arg"] == "production"


@pytest.mark.asyncio
async def test_handle_workflow_tool(tool_context):
    script = """
async def main(args):
    phase("Init")
    log("Workflow tool initialized")
    return {"calculated": 42 * 2}
"""
    res = await handle_workflow_tool(
        {
            "script": script,
            "meta": {"name": "CalculationWorkflow", "phases": ["Init"]},
        },
        tool_context,
    )
    assert res.ok is True
    assert res.name == "workflow"
    assert "CalculationWorkflow" in res.output
    assert res.metadata["output"]["calculated"] == 84


# =====================================================================
# 2. Ralph Automated Verification Engine Tests
# =====================================================================


def test_ralph_handoff_parser():
    # 1. Parse valid JSON code block
    json_text = """
```json
{
  "status": "complete",
  "summary": "All 42 tests passing.",
  "evidence": "pytest test_all.py returned exit code 0",
  "next_steps": "",
  "blocker": ""
}
```
"""
    h = _parse_handoff(json_text)
    assert h.status == "complete"
    assert "42 tests passing" in h.summary
    assert "exit code 0" in h.evidence

    # 2. Parse continue with next steps
    cont_text = """
{
  "status": "continue",
  "summary": "Fixed syntax error in parser.",
  "evidence": "flake8 passed.",
  "next_steps": "Add unit test for edge case."
}
"""
    h2 = _parse_handoff(cont_text)
    assert h2.status == "continue"
    assert h2.next_steps == "Add unit test for edge case."

    # 3. Fallback markdown section parser
    md_text = """
## Status: blocked
## Summary
Unable to connect to database.
## Blocker
Database server is offline.
"""
    h3 = _parse_handoff(md_text)
    assert h3.status == "blocked"
    assert "Unable to connect" in h3.summary
    assert "Database server is offline" in h3.blocker


@pytest.mark.asyncio
async def test_handle_ralph_tool_multiround(tool_context):
    # Mock client will return 'continue' in round 1 and 'complete' in round 2
    res = await handle_ralph_tool(
        {
            "objective": "Verify that all authentication edge cases are covered.",
            "max_rounds": 3,
        },
        tool_context,
    )

    assert res.ok is True
    assert res.name == "ralph"
    assert "VERIFIED COMPLETE" in res.output
    assert res.metadata["status"] == "complete"
    assert res.metadata["total_rounds"] == 2
    assert len(res.metadata["rounds"]) == 2
    assert res.metadata["rounds"][0]["handoff"]["status"] == "continue"
    assert res.metadata["rounds"][1]["handoff"]["status"] == "complete"


# =====================================================================
# 3. Agent Teams Coordination Seam & Swarm Tools Tests
# =====================================================================


def test_team_task_board():
    board = TeamTaskBoard()

    # 1. Create tasks with dependencies
    t1 = board.create_task(
        title="Design Schema", description="Design database tables", priority="high"
    )
    t2 = board.create_task(
        title="Implement Migration",
        description="Write Alembic migration",
        dependencies=[t1.task_id],
    )

    assert t1.status == "pending"
    assert t2.dependencies == [t1.task_id]
    assert board.can_start_task(t1.task_id) is True
    assert board.can_start_task(t2.task_id) is False

    # 2. Update task 1 to completed
    board.update_task(t1.task_id, status="completed", result="Schema schema.sql created")
    assert board.get_task(t1.task_id).status == "completed"
    assert board.can_start_task(t2.task_id) is True

    # 3. List tasks
    all_tasks = board.list_tasks()
    assert len(all_tasks) == 2
    pending_tasks = board.list_tasks(status="pending")
    assert len(pending_tasks) == 1
    assert pending_tasks[0].task_id == t2.task_id


def test_team_manager_messaging():
    reset_team_manager()
    mgr = get_team_manager()

    tm_alice = mgr.spawn_teammate(name="Alice", role="architect")
    tm_bob = mgr.spawn_teammate(name="Bob", role="coder")

    assert len(mgr.list_teammates()) == 2
    assert mgr.get_teammate("Alice").teammate_id == tm_alice.teammate_id
    assert mgr.get_teammate("bob").teammate_id == tm_bob.teammate_id

    # Send direct message
    msg = mgr.send_message(
        sender="Alice", recipient="Bob", content="Please implement the user model."
    )
    assert msg.recipient == "Bob"
    assert len(tm_bob.inbox) == 1
    assert tm_bob.inbox[0].content == "Please implement the user model."
    assert len(tm_alice.outbox) == 1

    # Send broadcast message
    mgr.send_message(sender="Alice", recipient="all", content="Standup in 5 minutes.")
    assert len(tm_alice.inbox) == 1
    assert len(tm_bob.inbox) == 2


@pytest.mark.asyncio
async def test_team_tool_handlers(tool_context):
    reset_team_manager()

    # 1. spawn_teammate tool
    spawn_res = await handle_spawn_teammate_tool(
        {"name": "Carol", "role": "reviewer", "mode": "read_only"},
        tool_context,
    )
    assert spawn_res.ok is True
    assert "Carol" in spawn_res.output
    carol_id = spawn_res.metadata["teammate_id"]

    # 2. team_task_create tool
    task_res = await handle_team_task_create_tool(
        {
            "title": "Review PR #42",
            "description": "Check security constraints",
            "assigned_to": carol_id,
            "priority": "high",
        },
        tool_context,
    )
    assert task_res.ok is True
    task_id = task_res.metadata["task_id"]

    # 3. team_task_get tool
    get_res = await handle_team_task_get_tool({"task_id": task_id}, tool_context)
    assert get_res.ok is True
    assert "Review PR #42" in get_res.output

    # 4. team_task_update tool
    update_res = await handle_team_task_update_tool(
        {"task_id": task_id, "status": "completed", "result": "PR approved with minor nitpicks."},
        tool_context,
    )
    assert update_res.ok is True
    assert update_res.metadata["status"] == "completed"

    # 5. team_task_list tool
    list_res = await handle_team_task_list_tool({"status": "completed"}, tool_context)
    assert list_res.ok is True
    assert len(list_res.metadata["tasks"]) == 1

    # 6. wait_agent tool
    # Carol has completed task / status
    get_team_manager().get_teammate(carol_id).status = "completed"
    wait_res = await handle_wait_agent_tool(
        {"agent_id": carol_id, "timeout_seconds": 2.0}, tool_context
    )
    assert wait_res.ok is True
    assert wait_res.metadata["status"] == "settled"


# =====================================================================
# 4. Tool Registry & Permissions Integration Tests
# =====================================================================


def test_phase2_tools_registry_and_permissions(temp_workspace):
    registry = get_tool_registry()

    phase2_tools = [
        "workflow",
        "ralph",
        "spawn_teammate",
        "team_task_create",
        "team_task_get",
        "team_task_list",
        "team_task_update",
        "wait_agent",
    ]

    for tool_name in phase2_tools:
        assert registry.has_tool(tool_name) is True, f"Tool '{tool_name}' missing from registry"
        tool_def = registry.get(tool_name)
        assert tool_def is not None
        openai_schema = tool_def.to_openai_schema()
        assert openai_schema["type"] == "function"
        assert openai_schema["function"]["name"] == tool_name

    # Check permission requests
    wf_perm = describe_tool_permission_request(
        session_id="s1",
        project_root=temp_workspace,
        tool_call={
            "id": "c1",
            "function": {"name": "workflow", "arguments": json.dumps({"script": "pass"})},
        },
    )
    assert "write-in-cwd" in wf_perm["scopes"]

    ralph_perm = describe_tool_permission_request(
        session_id="s1",
        project_root=temp_workspace,
        tool_call={
            "id": "c2",
            "function": {
                "name": "ralph",
                "arguments": json.dumps({"objective": "test", "mode": "general"}),
            },
        },
    )
    assert "write-in-cwd" in ralph_perm["scopes"]

    get_task_perm = describe_tool_permission_request(
        session_id="s1",
        project_root=temp_workspace,
        tool_call={
            "id": "c3",
            "function": {"name": "team_task_get", "arguments": json.dumps({"task_id": "task_1"})},
        },
    )
    assert get_task_perm["scopes"] == []


# =====================================================================
# 5. Error Handling and Edge Case Tests
# =====================================================================


@pytest.mark.asyncio
async def test_workflow_error_handling(tool_context):
    # 1. Missing script
    res = await handle_workflow_tool({}, tool_context)
    assert res.ok is False
    assert "Missing required parameter" in res.error

    # 2. Syntax / runtime error in script
    bad_script = """
async def main():
    raise ValueError("Intentional workflow failure")
"""
    res2 = await handle_workflow_tool({"script": bad_script}, tool_context)
    assert res2.ok is False
    assert "ValueError: Intentional workflow failure" in res2.error


@pytest.mark.asyncio
async def test_ralph_blocked_and_max_rounds(temp_workspace):
    def blocked_client():
        class MockCompletions:
            def create(self, **kwargs):
                content = json.dumps(
                    {
                        "status": "blocked",
                        "summary": "External dependency missing.",
                        "blocker": "Docker daemon not running.",
                    }
                )
                msg = type(
                    "M",
                    (),
                    {
                        "content": content,
                        "tool_calls": None,
                        "reasoning_content": None,
                        "refusal": None,
                    },
                )()
                usage = type(
                    "U", (), {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}
                )()
                return type(
                    "R", (), {"choices": [type("C", (), {"message": msg})()], "usage": usage}
                )()

        class MockChat:
            completions = MockCompletions()

        class MockClient:
            chat = MockChat()

        return {"client": MockClient(), "model": "gpt-5.6-luna"}

    ctx = ToolExecutionContext(
        session_id="test_ralph_block",
        project_root=temp_workspace,
        create_openai_client=blocked_client,
    )

    # 1. Blocked verification
    res = await handle_ralph_tool({"objective": "Deploy to staging", "max_rounds": 2}, ctx)
    assert res.ok is False
    assert res.metadata["status"] == "blocked"
    assert "Docker daemon not running" in res.metadata["final_verdict"]

    # 2. Missing objective
    res_empty = await handle_ralph_tool({}, ctx)
    assert res_empty.ok is False
    assert "Missing required argument 'objective'" in res_empty.error


@pytest.mark.asyncio
async def test_teams_error_handling_and_wait_agent(tool_context):
    reset_team_manager()

    # 1. Missing spawn parameters
    res_spawn = await handle_spawn_teammate_tool({}, tool_context)
    assert res_spawn.ok is False
    assert "required" in res_spawn.error

    # 2. Missing task parameters
    res_task = await handle_team_task_create_tool({}, tool_context)
    assert res_task.ok is False
    assert "title" in res_task.error

    # 3. Non-existent task get / update
    res_get = await handle_team_task_get_tool({"task_id": "non_existent"}, tool_context)
    assert res_get.ok is False
    assert "not found" in res_get.error

    res_update = await handle_team_task_update_tool(
        {"task_id": "non_existent", "status": "completed"}, tool_context
    )
    assert res_update.ok is False
    assert "not found" in res_update.error

    # 4. wait_agent timeout
    tm = get_team_manager().spawn_teammate(name="Dave", role="tester")
    tm.status = "working"
    wait_timeout = await handle_wait_agent_tool(
        {"agent_id": tm.teammate_id, "timeout_seconds": 0.1}, tool_context
    )
    assert wait_timeout.ok is False
    assert wait_timeout.metadata["status"] == "timeout"

    # 5. wait_agent for message settlement
    get_team_manager().send_message(sender="coordinator", recipient="Dave", content="Ping")
    wait_msg = await handle_wait_agent_tool(
        {"agent_id": tm.teammate_id, "wait_for": "message", "timeout_seconds": 1.0}, tool_context
    )
    assert wait_msg.ok is True
    assert wait_msg.metadata["status"] == "settled"
