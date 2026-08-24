"""Comprehensive Unit Tests for ToolRegistry and ToolExecutor standardized lifecycle."""

import asyncio
import json
import pytest
from coderai.core.tools.executor import ToolExecutor
from coderai.core.tools.registry import ToolRegistry
from coderai.core.permissions import (
    describe_tool_permission_request,
    evaluate_permission_scopes,
    permission_coverage_gaps,
)
from coderai.core.tools.types import (
    ToolDefinition,
    ToolExecutionHooks,
    ToolResult,
    ValidationError,
)


@pytest.mark.asyncio
async def test_tool_registry_builtins_use_canonical_names_only():
    registry = ToolRegistry()
    assert registry.has_tool("bash")
    assert registry.has_tool("read")
    assert registry.has_tool("write")
    assert registry.has_tool("edit")
    assert registry.has_tool("WebSearch")
    assert registry.has_tool("WebFetch")
    assert registry.has_tool("Task")
    assert registry.has_tool("subagent")
    assert registry.has_tool("job_list")
    assert registry.has_tool("job_output")
    assert registry.has_tool("job_kill")
    assert not registry.has_tool("Bash")
    assert not registry.has_tool("Read")
    assert not registry.has_tool("web_search")

    read_def = registry.get("read")
    assert read_def is not None
    assert read_def.name == "read"
    assert "file_path" in read_def.required


@pytest.mark.asyncio
async def test_tool_registry_validation_required_args():
    registry = ToolRegistry()

    # Missing required argument for 'read'
    with pytest.raises(ValidationError) as exc:
        registry.validate_arguments("read", {})
    assert "missing required argument 'file_path'" in str(exc.value)

    # Valid arguments
    args = registry.validate_arguments("read", {"file_path": "foo.py", "offset": 10})
    assert args["file_path"] == "foo.py"


@pytest.mark.asyncio
async def test_tool_registry_validation_types_and_enums():
    registry = ToolRegistry()

    # Type mismatch: string expected for file_path
    with pytest.raises(ValidationError) as exc:
        registry.validate_arguments("read", {"file_path": 12345})
    assert "must be a string" in str(exc.value)

    # Type mismatch: number expected for offset
    with pytest.raises(ValidationError) as exc:
        registry.validate_arguments("read", {"file_path": "foo.py", "offset": "not-a-number"})
    assert "must be a number" in str(exc.value)

    # Enum mismatch in Task mode
    with pytest.raises(ValidationError) as exc:
        registry.validate_arguments(
            "Task",
            {"description": "test", "prompt": "do something", "mode": "invalid_mode"},
        )
    assert "invalid value" in str(exc.value)


@pytest.mark.asyncio
async def test_tool_registry_custom_registration():
    registry = ToolRegistry()

    async def custom_handler(args, context):
        return ToolResult(ok=True, name="custom_tool", output=f"Calculated: {args['x'] * 2}")

    custom_def = ToolDefinition(
        name="custom_calc",
        aliases=["calc"],
        description="Multiply number by two",
        parameters={"x": {"type": "number", "description": "Number to double"}},
        required=["x"],
        handler=custom_handler,
    )
    registry.register(custom_def)

    assert registry.has_tool("custom_calc")
    assert registry.has_tool("calc")

    schema = custom_def.to_openai_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "custom_calc"
    assert schema["function"]["parameters"]["required"] == ["x"]


@pytest.mark.asyncio
async def test_tool_executor_validation_error_recovery(tmp_path):
    executor = ToolExecutor(project_root=str(tmp_path))

    # Invalid tool call: missing required 'file_path'
    tool_call = {
        "id": "tc_1",
        "type": "function",
        "function": {
            "name": "read",
            "arguments": json.dumps({"offset": 10}),
        },
    }

    result = await executor.execute_tool_call("sess_1", tool_call)
    assert not result.ok
    assert "ValidationError" in (result.error or "")


@pytest.mark.asyncio
async def test_tool_executor_malformed_json_recovery(tmp_path):
    executor = ToolExecutor(project_root=str(tmp_path))

    tool_call = {
        "id": "tc_bad_json",
        "type": "function",
        "function": {
            "name": "read",
            "arguments": "{bad_json: missing_quotes",
        },
    }

    result = await executor.execute_tool_call("sess_1", tool_call)
    assert not result.ok
    assert "InputParseError" in (result.error or "")


@pytest.mark.asyncio
async def test_tool_executor_sequential_and_parallel_dispatch(tmp_path):
    registry = ToolRegistry()

    call_order = []

    async def fast_tool(args, ctx):
        call_order.append(f"fast_{args['id']}")
        return ToolResult(ok=True, name="fast", output=f"fast {args['id']}")

    async def slow_tool(args, ctx):
        await asyncio.sleep(0.05)
        call_order.append(f"slow_{args['id']}")
        return ToolResult(ok=True, name="slow", output=f"slow {args['id']}")

    registry.register(
        ToolDefinition(
            name="fast", parameters={"id": {"type": "number"}}, required=["id"], handler=fast_tool
        )
    )
    registry.register(
        ToolDefinition(
            name="slow", parameters={"id": {"type": "number"}}, required=["id"], handler=slow_tool
        )
    )

    executor = ToolExecutor(project_root=str(tmp_path), registry=registry)

    calls = [
        {
            "id": "c1",
            "type": "function",
            "function": {"name": "slow", "arguments": json.dumps({"id": 1})},
        },
        {
            "id": "c2",
            "type": "function",
            "function": {"name": "fast", "arguments": json.dumps({"id": 2})},
        },
    ]

    # Test sequential
    call_order.clear()
    res_seq = await executor.execute_tool_calls("sess_seq", calls, parallel=False)
    assert len(res_seq) == 2
    assert call_order == ["slow_1", "fast_2"]

    # Test parallel (slow will finish after fast, but results match order of calls)
    call_order.clear()
    res_par = await executor.execute_tool_calls("sess_par", calls, parallel=True)
    assert len(res_par) == 2
    assert res_par[0]["toolCallId"] == "c1"
    assert res_par[1]["toolCallId"] == "c2"
    assert call_order == ["fast_2", "slow_1"]


@pytest.mark.asyncio
async def test_tool_executor_interruption_should_stop(tmp_path):
    registry = ToolRegistry()
    executed = []

    async def step_tool(args, ctx):
        executed.append(args["n"])
        return ToolResult(ok=True, name="step", output=f"step {args['n']}")

    registry.register(
        ToolDefinition(
            name="step", parameters={"n": {"type": "number"}}, required=["n"], handler=step_tool
        )
    )

    executor = ToolExecutor(project_root=str(tmp_path), registry=registry)

    stopped = False

    def check_stop():
        nonlocal stopped
        return stopped

    hooks = ToolExecutionHooks(should_stop=check_stop)

    calls = [
        {
            "id": "1",
            "type": "function",
            "function": {"name": "step", "arguments": json.dumps({"n": 1})},
        },
        {
            "id": "2",
            "type": "function",
            "function": {"name": "step", "arguments": json.dumps({"n": 2})},
        },
    ]

    stopped = True
    executions = await executor.execute_tool_calls("sess_stop", calls, hooks=hooks)
    assert len(executions) == 0
    assert len(executed) == 0


def test_tool_registry_openai_schema_sanitization():
    registry = ToolRegistry()
    schemas = registry.to_openai_schemas()
    for s in schemas:
        schema_json = json.dumps(s)
        assert '"uniqueItems"' not in schema_json
        assert '"$schema"' not in schema_json
        assert '"$id"' not in schema_json

    bash_def = registry.get("bash")
    assert bash_def is not None
    bash_schema = bash_def.to_openai_schema()
    assert "uniqueItems" not in bash_schema["function"]["parameters"]["properties"]["sideEffects"]


def test_tool_registry_canonical_presets():
    registry = ToolRegistry()
    core_names = {
        schema["function"]["name"]
        for schema in registry.to_openai_schemas(options={"preset": "core"})
    }
    shell_edit_names = {
        schema["function"]["name"]
        for schema in registry.to_openai_schemas(options={"preset": "shell_edit"})
    }
    assert core_names == {"bash", "str_replace_editor", "edit", "read", "write", "glob", "grep"}
    assert shell_edit_names == {"bash", "str_replace_editor"}
    assert len(registry.to_openai_schemas(options={"preset": "full"})) > len(core_names)


def test_registered_tools_have_permission_coverage():
    assert permission_coverage_gaps() == set()


def test_unknown_registered_tool_fails_closed_but_safe_tool_does_not(tmp_path):
    from coderai.core.tools.registry import get_tool_registry

    registry = get_tool_registry()
    unregister = registry.register(
        ToolDefinition(name="unclassified_dynamic_tool", aliases=["dynamic_alias"])
    )
    try:
        request = describe_tool_permission_request(
            session_id="session",
            project_root=str(tmp_path),
            tool_call={
                "id": "dynamic",
                "function": {"name": "unclassified_dynamic_tool", "arguments": "{}"},
            },
        )
        assert request["scopes"] == ["unknown"]
        assert evaluate_permission_scopes(request["scopes"]) == "ask"

        aliased_request = describe_tool_permission_request(
            session_id="session",
            project_root=str(tmp_path),
            tool_call={"id": "alias", "function": {"name": "dynamic_alias", "arguments": "{}"}},
        )
        assert aliased_request["name"] == "unclassified_dynamic_tool"
        assert aliased_request["scopes"] == ["unknown"]

        safe_request = describe_tool_permission_request(
            session_id="session",
            project_root=str(tmp_path),
            tool_call={"id": "safe", "function": {"name": "job_list", "arguments": "{}"}},
        )
        assert safe_request["scopes"] == []
        assert evaluate_permission_scopes(safe_request["scopes"]) == "allow"
    finally:
        unregister()
