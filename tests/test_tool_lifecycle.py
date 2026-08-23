"""Comprehensive test suite for CoderAI modernized tool lifecycle & architecture.

Tests DeepSeek Harness gold-standard parity:
- Declarative schema validation & type constraints
- Staged execution lifecycle (prepare -> guard -> dispatch -> post-execute -> finalize -> finish)
- Concurrency classification & parallel safety
- Scoped tool layers, capability restrictions, and monotonic guards
- Cancellation signals & timeout budgets
- Deferred context & turn conclusion
- Dynamic unregistration disposers & change listeners
"""

import asyncio
import json
from typing import Any
import pytest

from coderai.core.tools.executor import ToolExecutor
from coderai.core.tools.registry import ToolRegistry, get_tool_registry
from coderai.core.tools.schema import (
    assert_supported_json_schema,
    define_tool,
    validate_json_schema_value,
)
from coderai.core.tools.types import (
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionFollowUpMessage,
    ToolExecutionHooks,
    ToolResult,
)


# ==============================================================================
# 1. JSON Schema & Declarative DSL Tests
# ==============================================================================


def test_assert_supported_json_schema():
    valid_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "count": {"type": "integer"},
            "flags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["path"],
    }
    assert_supported_json_schema(valid_schema)

    with pytest.raises(TypeError):
        assert_supported_json_schema("not-a-dict")  # type: ignore

    with pytest.raises(ValueError):
        assert_supported_json_schema({"type": "invalid_type"})


def test_validate_json_schema_value_types():
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 2},
            "age": {"type": "integer", "minimum": 0, "maximum": 120},
            "ratio": {"type": "number"},
            "active": {"type": "boolean"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "role": {"type": "string", "enum": ["admin", "user", "guest"]},
        },
        "required": ["name", "age"],
        "additionalProperties": False,
    }

    # Valid value
    valid = {
        "name": "Alice",
        "age": 30,
        "ratio": 1.5,
        "active": True,
        "tags": ["python", "ai"],
        "role": "admin",
    }
    violations = validate_json_schema_value(schema, valid)
    assert len(violations) == 0

    # Invalid: missing required 'age', type mismatch on 'name', invalid enum
    invalid = {
        "name": 123,
        "role": "superuser",
        "extra_field": "disallowed",
    }
    violations = validate_json_schema_value(schema, invalid)
    assert any("missing required property 'age'" in v for v in violations)
    assert any("expected string, got int" in v for v in violations)
    assert any("invalid value 'superuser'" in v for v in violations)
    assert any("unexpected property 'extra_field'" in v for v in violations)


def test_define_tool_dsl():
    tool = define_tool(
        name="custom_scanner",
        description="Scans files for patterns",
        parameters={
            "dir_path": {"type": "string", "required": True, "description": "Target folder"},
            "max_depth": {"type": "integer", "description": "Max directory depth"},
        },
        category="filesystem",
        is_concurrency_safe=True,
        timeout_ms=5000,
    )
    assert tool.name == "custom_scanner"
    assert "dir_path" in tool.required
    assert tool.category == "filesystem"
    assert tool.is_concurrency_safe is True
    assert tool.timeout_ms == 5000

    openai_schema = tool.to_openai_schema()
    assert openai_schema["type"] == "function"
    assert openai_schema["function"]["name"] == "custom_scanner"
    assert openai_schema["function"]["parameters"]["required"] == ["dir_path"]


# ==============================================================================
# 2. Scoped Layers, Restrictions & Monotonic Guards
# ==============================================================================


def test_registry_scoping_and_restrictions():
    registry = ToolRegistry()

    # Session 1: Read-Only subagent restriction
    disposer = registry.restrict(
        {"deny": ["write", "edit", "str_replace_editor", "bash"]},
        scope="subagent_ro_session",
    )

    # In subagent_ro_session, read and glob are available, but write/bash are masked
    assert registry.has_tool("read", scope="subagent_ro_session")
    assert registry.has_tool("glob", scope="subagent_ro_session")
    assert not registry.has_tool("write", scope="subagent_ro_session")
    assert not registry.has_tool("bash", scope="subagent_ro_session")

    # In global/main session, write and bash remain available
    assert registry.has_tool("write")
    assert registry.has_tool("bash")

    # Disposing the restriction lifts it
    disposer()
    assert registry.has_tool("write", scope="subagent_ro_session")


def test_registry_monotonic_guards():
    registry = ToolRegistry()

    def security_guard(tool_def: ToolDefinition, args: dict[str, Any], context: Any) -> str | None:
        if tool_def.name == "bash" and "rm -rf" in str(args.get("command", "")):
            return "Destructive command rejected by security guard"
        return None

    guard_disposer = registry.guard(security_guard)

    tool = registry.get("bash")
    assert tool is not None

    # Check guard invocation
    layer = registry._global_layer
    assert len(layer.guards) == 1
    reason = layer.guards[0](tool, {"command": "rm -rf /"}, None)
    assert reason == "Destructive command rejected by security guard"

    reason_safe = layer.guards[0](tool, {"command": "git status"}, None)
    assert reason_safe is None

    guard_disposer()
    assert len(layer.guards) == 0


def test_registry_dynamic_unregistration_and_change_events():
    registry = ToolRegistry()
    events = []

    def on_change():
        events.append("changed")

    unsub = registry.on_change(on_change)

    tool = define_tool(
        name="temp_probe",
        description="Temporary probe tool",
        parameters={},
        handler=lambda args, ctx: ToolResult(ok=True, name="temp_probe", output="ok"),
    )

    unreg = registry.register(tool)
    assert registry.has_tool("temp_probe")
    assert len(events) == 1

    # Unregister via returned disposer
    unreg()
    assert not registry.has_tool("temp_probe")
    assert len(events) == 2

    unsub()


# ==============================================================================
# 3. Execution Lifecycle, Hooks & Staged Scheduler
# ==============================================================================


@pytest.mark.asyncio
async def test_tool_executor_full_staged_lifecycle(tmp_path):
    registry = ToolRegistry()
    lifecycle_log = []

    def finalize_transform(ctx: ToolExecutionContext, result: ToolResult) -> str | None:
        lifecycle_log.append("finalize_content")
        return f"{result.output} [FINALIZED]"

    def present_result(args: dict[str, Any], result: ToolResult) -> dict[str, Any]:
        lifecycle_log.append("present_result")
        return {"summary": "done", "bytes": len(result.output or "")}

    async def sample_handler(args: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        lifecycle_log.append("handler_run")
        context.defer_context(ToolExecutionFollowUpMessage(role="system", content="Deferred tip"))
        context.conclude_turn()
        return ToolResult(ok=True, name="lifecycle_demo", output=f"Hello {args['target']}")

    tool = define_tool(
        name="lifecycle_demo",
        description="Lifecycle demonstration tool",
        parameters={"target": {"type": "string", "required": True}},
        handler=sample_handler,
        finalize_content=finalize_transform,
        present_result=present_result,
        is_concurrency_safe=True,
    )
    registry.register(tool)

    executor = ToolExecutor(project_root=str(tmp_path), registry=registry)

    def pre_hook(name: str, args: dict[str, Any], ctx: ToolExecutionContext) -> str:
        lifecycle_log.append("pre_execute")
        return "allow"

    def post_hook(
        name: str, args: dict[str, Any], result: ToolResult, ctx: ToolExecutionContext
    ) -> ToolResult:
        lifecycle_log.append("post_execute")
        return result

    hooks = ToolExecutionHooks(pre_execute=pre_hook, post_execute=post_hook)

    call = {
        "id": "c_life",
        "type": "function",
        "function": {
            "name": "lifecycle_demo",
            "arguments": json.dumps({"target": "DeepSeek"}),
        },
    }

    result = await executor.execute_tool_call("sess_life", call, hooks=hooks)

    assert result.ok
    assert "[FINALIZED]" in (result.output or "")
    assert result.concludes_turn is True
    assert len(result.follow_up_messages) == 1
    assert result.metadata is not None
    assert "presentation" in result.metadata
    assert result.metadata["presentation"]["summary"] == "done"

    # Verify correct lifecycle order
    assert lifecycle_log == [
        "pre_execute",
        "handler_run",
        "finalize_content",
        "present_result",
        "post_execute",
    ]


@pytest.mark.asyncio
async def test_tool_executor_fail_closed_permission_and_guards(tmp_path):
    registry = ToolRegistry()

    async def dummy_handler(args, ctx):
        return ToolResult(ok=True, name="dummy", output="executed")

    registry.register(define_tool(name="dummy", parameters={}, handler=dummy_handler))

    executor = ToolExecutor(project_root=str(tmp_path), registry=registry)

    # 1. Denied via permission_decision
    hooks_denied = ToolExecutionHooks(permission_decision="deny")
    res_deny = await executor.execute_tool_call(
        "s1",
        {"id": "1", "function": {"name": "dummy", "arguments": "{}"}},
        hooks=hooks_denied,
    )
    assert not res_deny.ok
    assert "PermissionDenied" in (res_deny.error or "")

    # 2. Denied via guard
    def guard_block(name, args, ctx):
        return "deny"

    hooks_guard = ToolExecutionHooks(guards=[guard_block])
    res_guard = await executor.execute_tool_call(
        "s1",
        {"id": "2", "function": {"name": "dummy", "arguments": "{}"}},
        hooks=hooks_guard,
    )
    assert not res_guard.ok
    assert "GuardDenied" in (res_guard.error or "")


@pytest.mark.asyncio
async def test_tool_executor_timeout_enforcement(tmp_path):
    registry = ToolRegistry()

    async def slow_handler(args, ctx):
        await asyncio.sleep(0.5)
        return ToolResult(ok=True, name="slow", output="finished")

    registry.register(define_tool(name="slow", parameters={}, handler=slow_handler, timeout_ms=50))

    executor = ToolExecutor(project_root=str(tmp_path), registry=registry)

    res = await executor.execute_tool_call(
        "s_timeout",
        {"id": "t1", "function": {"name": "slow", "arguments": "{}"}},
    )
    assert not res.ok
    assert "TOOL_TIMEOUT" in (res.error or "")


@pytest.mark.asyncio
async def test_tool_concurrency_classification():
    registry = get_tool_registry()

    # Read, glob, grep, session_query, lsp are concurrency-safe
    read_def = registry.get("read")
    assert read_def is not None
    assert read_def.check_concurrency_safe({}) is True

    glob_def = registry.get("glob")
    assert glob_def is not None
    assert glob_def.check_concurrency_safe({}) is True

    # Write, edit, bash are mutating/exclusive
    write_def = registry.get("write")
    assert write_def is not None
    assert write_def.check_concurrency_safe({}) is False

    bash_def = registry.get("bash")
    assert bash_def is not None
    assert bash_def.check_concurrency_safe({}) is False

    # str_replace_editor is concurrency safe only for 'view' command
    sre_def = registry.get("str_replace_editor")
    assert sre_def is not None
    assert sre_def.check_concurrency_safe({"command": "view"}) is True
    assert sre_def.check_concurrency_safe({"command": "str_replace"}) is False
