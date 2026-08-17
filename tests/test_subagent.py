"""Comprehensive tests for CoderAI's Sub-Agent Architecture and Execution Engine."""

import json
import pathlib
import pytest

from coderai.core.subagent import (
    MAX_SUBAGENT_DEPTH,
    SubAgentManager,
    SubAgentResult,
    SubAgentSpec,
)
from coderai.core.tools.executor import ToolExecutor
from coderai.core.tools.subagent import handle_subagent_tool
from coderai.core.tools.types import ToolExecutionContext


def _make_mock_client(responses):
    """Helper to mock OpenAI client returning pre-canned responses sequentially."""
    idx = 0

    class Completions:
        def create(self, **kwargs):
            nonlocal idx
            if idx < len(responses):
                res = responses[idx]
                idx += 1
                return res
            return _text_response("Default conclusion.")

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    return Client()


def _text_response(content):
    message = type(
        "M",
        (),
        {"content": content, "tool_calls": None, "reasoning_content": None, "refusal": None},
    )()
    usage = type("U", (), {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})()
    return type("R", (), {"choices": [type("C", (), {"message": message})()], "usage": usage})()


def _tool_call_response(tool_calls, content=""):
    message = type(
        "M",
        (),
        {"content": content, "tool_calls": tool_calls, "reasoning_content": None, "refusal": None},
    )()
    usage = type("U", (), {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20})()
    return type("R", (), {"choices": [type("C", (), {"message": message})()], "usage": usage})()


def _tc(cid, name, args):
    return type(
        "TC",
        (),
        {"id": cid, "function": type("F", (), {"name": name, "arguments": json.dumps(args)})()},
    )()


@pytest.mark.asyncio
async def test_subagent_spec_and_result_formatting():
    spec = SubAgentSpec(
        description="Search repo",
        prompt="Find all auth handlers",
        mode="read_only",
        timeout_seconds=30.0,
    )
    assert spec.description == "Search repo"
    assert spec.mode == "read_only"
    assert spec.timeout_seconds == 30.0

    res = SubAgentResult(
        task_id="task_123",
        session_id="sub_root_task_123",
        status="completed",
        summary="Found 3 auth files: login.py, auth.py, token.py.",
        active_tokens=45,
        total_tokens=90,
        iterations=2,
        tool_calls_count=3,
        artifacts=["login.py", "auth.py"],
    )
    md = res.format_markdown()
    assert "### Sub-Agent Task Result [task_123] — ✅ COMPLETED" in md
    assert "Found 3 auth files" in md
    assert "- `login.py`" in md
    assert res.to_dict()["status"] == "completed"


@pytest.mark.asyncio
async def test_subagent_spawn_and_completion(tmp_path: pathlib.Path):
    (tmp_path / "main.py").write_text("print('hello world')\n")

    responses = [
        _tool_call_response(
            [_tc("call_1", "read", {"file_path": str(tmp_path / "main.py")})],
            content="I will read main.py",
        ),
        _text_response("main.py contains a hello world print statement."),
    ]
    client = _make_mock_client(responses)

    manager = SubAgentManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {
            "client": client,
            "model": "gpt-4o",
            "thinkingEnabled": False,
            "reasoningEffort": "max",
        },
    )

    spec = SubAgentSpec(
        description="Inspect main",
        prompt="What does main.py do?",
        mode="read_only",
    )

    result = await manager.spawn_subagent(spec)
    assert result.status == "completed"
    assert "hello world" in result.summary
    assert result.iterations == 2
    assert result.tool_calls_count == 1
    assert any("main.py" in art for art in result.artifacts)


@pytest.mark.asyncio
async def test_subagent_read_only_permission_sandboxing(tmp_path: pathlib.Path):
    responses = [
        _tool_call_response(
            [
                _tc(
                    "call_w1",
                    "write",
                    {"file_path": str(tmp_path / "hack.py"), "content": "malicious"},
                )
            ],
            content="Attempting to write file",
        ),
        _text_response("Could not write file due to read_only sandbox."),
    ]
    client = _make_mock_client(responses)

    manager = SubAgentManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {
            "client": client,
            "model": "gpt-4o",
        },
    )

    spec = SubAgentSpec(
        description="Attempt write",
        prompt="Write a file",
        mode="read_only",
    )

    result = await manager.spawn_subagent(spec)
    assert result.status == "completed"
    # Ensure hack.py was NOT written to disk
    assert not (tmp_path / "hack.py").exists()


@pytest.mark.asyncio
async def test_subagent_recursion_depth_limit(tmp_path: pathlib.Path):
    manager = SubAgentManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": _make_mock_client([]), "model": "gpt-4o"},
    )

    spec = SubAgentSpec(
        description="Nested subagent",
        prompt="Spawn another child",
        depth=MAX_SUBAGENT_DEPTH + 1,
    )

    result = await manager.spawn_subagent(spec)
    assert result.status == "failed"
    assert "nesting depth exceeded" in result.summary.lower()


@pytest.mark.asyncio
async def test_parallel_subagent_execution(tmp_path: pathlib.Path):
    (tmp_path / "a.txt").write_text("File A content")
    (tmp_path / "b.txt").write_text("File B content")

    def client_factory():
        return {
            "client": _make_mock_client([_text_response("Analyzed file.")]),
            "model": "gpt-4o",
        }

    manager = SubAgentManager(
        project_root=str(tmp_path),
        create_openai_client=client_factory,
    )

    specs = [
        SubAgentSpec(description="Analyze A", prompt="Analyze a.txt", task_id="t_a"),
        SubAgentSpec(description="Analyze B", prompt="Analyze b.txt", task_id="t_b"),
        SubAgentSpec(description="Analyze C", prompt="Analyze c.txt", task_id="t_c"),
    ]

    results = await manager.run_parallel_subagents(specs, max_concurrency=2)
    assert len(results) == 3
    assert all(r.status == "completed" for r in results)
    assert {r.task_id for r in results} == {"t_a", "t_b", "t_c"}


@pytest.mark.asyncio
async def test_subagent_timeout_handling(tmp_path: pathlib.Path):
    class SlowCompletions:
        def create(self, **kwargs):
            import time

            time.sleep(0.3)
            return _text_response("Slow result")

    class SlowChat:
        completions = SlowCompletions()

    class SlowClient:
        chat = SlowChat()

    manager = SubAgentManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": SlowClient(), "model": "gpt-4o"},
    )

    spec = SubAgentSpec(
        description="Slow task",
        prompt="Do something slow",
        timeout_seconds=0.1,
    )

    result = await manager.spawn_subagent(spec)
    assert result.status == "timeout"
    assert "timed out" in result.summary.lower()


@pytest.mark.asyncio
async def test_subagent_cancellation(tmp_path: pathlib.Path):
    manager = SubAgentManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": _make_mock_client([]), "model": "gpt-4o"},
    )

    # Cancel all
    manager.cancel_all()
    # Cancel unknown does not error
    manager.cancel_subagent("nonexistent_session")


@pytest.mark.asyncio
async def test_task_tool_handler(tmp_path: pathlib.Path):
    client = _make_mock_client([_text_response("Task completed successfully.")])

    def client_factory():
        return {"client": client, "model": "gpt-4o"}

    context = ToolExecutionContext(
        session_id="parent_session_123",
        project_root=str(tmp_path),
        create_openai_client=client_factory,
    )

    # Missing description validation
    res_err1 = await handle_subagent_tool({"prompt": "Do work"}, context)
    assert not res_err1.ok
    assert "description" in res_err1.error.lower()

    # Missing prompt validation
    res_err2 = await handle_subagent_tool({"description": "Work"}, context)
    assert not res_err2.ok
    assert "prompt" in res_err2.error.lower()

    # Successful execution
    res_ok = await handle_subagent_tool(
        {"description": "Search code", "prompt": "Find functions", "mode": "read_only"},
        context,
    )
    assert res_ok.ok
    assert res_ok.name == "Task"
    assert "Task completed successfully" in (res_ok.output or "")
    assert res_ok.metadata["status"] == "completed"


@pytest.mark.asyncio
async def test_task_tool_executor_dispatch(tmp_path: pathlib.Path):
    client = _make_mock_client([_text_response("Executor dispatch succeeded.")])

    def client_factory():
        return {"client": client, "model": "gpt-4o"}

    executor = ToolExecutor(str(tmp_path), client_factory)
    executions = await executor.execute_tool_calls(
        "parent_session_1",
        [
            {
                "id": "tc_task_1",
                "type": "function",
                "function": {
                    "name": "Task",
                    "arguments": json.dumps({"description": "Explore", "prompt": "Explore files"}),
                },
            }
        ],
    )

    assert len(executions) == 1
    assert executions[0]["toolCallId"] == "tc_task_1"
    parsed_res = json.loads(executions[0]["content"])
    assert parsed_res["ok"] is True
    assert "Executor dispatch succeeded" in parsed_res["output"]
