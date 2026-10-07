"""Regression tests for tool policy, approval, and ownership boundaries."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from coderai.soul.approval import (
    compute_tool_call_permissions,
    apply_auto_approve_to_permission_plan,
)
from coderai.tools.legacy.authorization import build_tool_authorizations
from coderai.tools.legacy.executor import ToolExecutor
from coderai.tools.legacy.registry import ToolRegistry
from coderai.tools.legacy.schema import define_tool, assert_supported_json_schema
from coderai.tools.legacy.sanitizer import sanitize_tool_output
from coderai.tools.legacy.types import (
    ToolExecutionContext,
    ToolExecutionFollowUpMessage,
    ToolResult,
    ValidationError,
)


def call(name, args, ident="call-1"):
    return {
        "id": ident,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def test_global_masks_cannot_be_widened_by_scoped_overrides():
    registry = ToolRegistry()
    registry.register(define_tool("custom", aliases=["alias"], handler=lambda a, c: "ok"))
    dispose = registry.restrict({"allow": ["alias"]})
    registry.register(define_tool("write", handler=lambda a, c: "bad"), scope="child")
    assert registry.get("write", "child") is None
    assert {t.name for t in registry.list_tools("child")} == {"custom"}
    assert registry.get("alias", "child").name == "custom"
    dispose()
    assert registry.get("write", "child") is not None


def test_nested_schema_and_no_remote_reference_resolution():
    registry = ToolRegistry()
    with pytest.raises(ValidationError):
        registry.validate_arguments(
            "AskUserQuestion", {"questions": [{"question": "Q", "options": ["bad"]}]}
        )
    schema = {
        "type": "object",
        "$defs": {"value": {"type": ["string", "null"]}},
        "properties": {"x": {"$ref": "#/$defs/value"}},
    }
    assert_supported_json_schema(schema)
    with pytest.raises(ValueError, match="Remote schema reference"):
        assert_supported_json_schema({"$ref": "https://untrusted.invalid/schema"})


def test_typed_followups_are_sanitized():
    token = "sk-" + "X" * 30
    result = ToolResult(
        ok=True,
        name="test",
        follow_up_messages=[
            ToolExecutionFollowUpMessage(content=token, content_params={"text": token})
        ],
    )
    sanitize_tool_output(result)
    assert token not in result.follow_up_messages[0].content
    assert token not in result.follow_up_messages[0].content_params["text"]


async def test_foreign_job_is_rejected_before_cancellation(tmp_path, monkeypatch):
    from coderai.tools.background import handle_job_kill_tool

    store = MagicMock()
    store.get.return_value = None
    manager, task = MagicMock(), MagicMock()
    monkeypatch.setattr("coderai.tools.background.get_job_store", lambda: store)
    monkeypatch.setattr(
        "coderai.tools.agent.subagent_job_tasks",
        lambda: {"foreign": (manager, "foreign-session", task)},
    )
    result = await handle_job_kill_tool(
        {"job_id": "foreign"}, ToolExecutionContext("caller", str(tmp_path))
    )
    assert not result.ok
    manager.cancel_subagent.assert_not_called()
    task.cancel.assert_not_called()
    store.kill.assert_not_called()


async def test_team_tasks_are_scoped_to_the_root_chat(tmp_path, monkeypatch):
    from coderai.teams.manager import TeamManager
    from coderai.teams.tools import (
        handle_team_task_create_tool,
        handle_team_task_get_tool,
        handle_team_task_list_tool,
        handle_team_task_update_tool,
    )

    manager = TeamManager()
    monkeypatch.setattr("coderai.teams.tools.get_team_manager", lambda: manager)
    one = ToolExecutionContext("one", str(tmp_path))
    other = ToolExecutionContext("other", str(tmp_path))
    created = await handle_team_task_create_tool({"title": "Private"}, one)
    ident = created.metadata["task_id"]
    assert (await handle_team_task_get_tool({"task_id": ident}, one)).ok
    assert not (await handle_team_task_get_tool({"task_id": ident}, other)).ok
    assert not (
        await handle_team_task_update_tool({"task_id": ident, "status": "completed"}, other)
    ).ok
    assert (await handle_team_task_list_tool({}, other)).metadata["tasks"] == []
    assert manager.task_board.get_task(ident).status == "pending"


async def test_plan_policy_blocks_external_mutation_and_preserves_plan_file(tmp_path, monkeypatch):
    executor = ToolExecutor(project_root=str(tmp_path))
    hooks = {
        "plan_mode": True,
        "session_manager": SimpleNamespace(get_resolved_settings=lambda: {}),
    }
    monkeypatch.setattr(
        "coderai.hooks.run_hook_point", lambda *a, **k: SimpleNamespace(to_dict=lambda: {})
    )
    context = executor._build_execution_context("s", call("mcp__test__mutate", {}), hooks)
    assert executor._pre_execute_deny("mcp__test__mutate", {}, context, hooks) is not None
    context.tool_call = call("bash", {"command": "echo test", "sideEffects": []})
    assert (
        executor._pre_execute_deny(
            "bash", {"command": "echo test", "sideEffects": []}, context, hooks
        )
        is None
    )
    assert context.sandbox_mode == "read-only"
    plan = tmp_path / ".coderai" / "plans" / "s.md"
    result = await executor.execute_tool_call(
        "s", call("write", {"file_path": str(plan), "content": "# Plan"}), hooks
    )
    assert result.ok, result.error
    assert plan.read_text() == "# Plan"
    blocked = await executor.execute_tool_call(
        "s", call("write", {"file_path": str(tmp_path / "code.py"), "content": "bad"}), hooks
    )
    assert not blocked.ok


async def test_exit_plan_requires_bound_acceptance_and_rejects_changed_plan(tmp_path):
    executor = ToolExecutor(project_root=str(tmp_path))
    request = call("exit_plan_mode", {"plan": "# Plan\nDo work"})
    pending = await executor.execute_tool_call("s", request, {"plan_mode": True})
    assert not pending.ok and not pending.metadata.get("exitPlanMode")
    plan = compute_tool_call_permissions(
        session_id="s", project_root=str(tmp_path), tool_calls=[request]
    )
    assert plan["askPermissions"][0]["requiresExplicitApproval"]
    assert apply_auto_approve_to_permission_plan(plan)["askPermissions"]
    grants = build_tool_authorizations(
        "s",
        str(tmp_path),
        [request],
        [{"toolCallId": "call-1", "permission": "allow"}],
        plan["askPermissions"],
        None,
    )
    accepted = await executor.execute_tool_call(
        "s", request, {"plan_mode": True, "authorizations": grants}
    )
    assert accepted.ok and accepted.metadata["exitPlanMode"]
    changed = await executor.execute_tool_call(
        "s",
        call("exit_plan_mode", {"plan": "# Changed"}),
        {"plan_mode": True, "authorizations": grants},
    )
    assert not changed.ok


async def test_descendants_share_team_but_other_projects_do_not(tmp_path, monkeypatch):
    from coderai.subagents.core import AgentHandle
    from coderai.teams.manager import TeamManager
    from coderai.teams.tools import handle_team_task_create_tool, handle_team_task_get_tool

    handles = [
        AgentHandle(
            id="child",
            parent_session_id="root",
            description="child",
            mode="read_only",
            run_session_id="child-session",
        )
    ]
    monkeypatch.setattr(
        __import__("importlib").import_module("coderai.subagents.core"),
        "get_agent_registry",
        lambda: SimpleNamespace(list=lambda: handles),
    )
    manager = TeamManager()
    monkeypatch.setattr("coderai.teams.tools.get_team_manager", lambda: manager)
    root = ToolExecutionContext("root", str(tmp_path))
    result = await handle_team_task_create_tool({"title": "Shared"}, root)
    task = {"task_id": result.metadata["task_id"]}
    assert (
        await handle_team_task_get_tool(task, ToolExecutionContext("child-session", str(tmp_path)))
    ).ok
    assert not (
        await handle_team_task_get_tool(
            task, ToolExecutionContext("child-session", str(tmp_path / "other"))
        )
    ).ok


async def test_changed_plan_file_invalidates_approval(tmp_path):
    plan_file = tmp_path / ".coderai" / "plans" / "s.md"
    plan_file.parent.mkdir(parents=True)
    plan_file.write_text("# Original plan")
    request = call("exit_plan_mode", {"summary": "Review plan"})
    plan = compute_tool_call_permissions(
        session_id="s", project_root=str(tmp_path), tool_calls=[request]
    )
    grants = build_tool_authorizations(
        "s",
        str(tmp_path),
        [request],
        [{"toolCallId": "call-1", "permission": "allow"}],
        plan["askPermissions"],
        None,
    )
    plan_file.write_text("# Different work")
    result = await ToolExecutor(str(tmp_path)).execute_tool_call(
        "s", request, {"plan_mode": True, "authorizations": grants}
    )
    assert not result.ok and not result.metadata.get("exitPlanMode")


async def test_plan_directory_symlinks_cannot_grant_file_access(tmp_path):
    external = tmp_path / "outside"
    external.mkdir()
    (tmp_path / ".coderai").mkdir()
    try:
        (tmp_path / ".coderai" / "plans").symlink_to(external, target_is_directory=True)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege unavailable")
        raise
    result = await ToolExecutor(str(tmp_path)).execute_tool_call(
        "s",
        call(
            "write", {"file_path": str(tmp_path / ".coderai" / "plans" / "s.md"), "content": "bad"}
        ),
        {"plan_mode": True},
    )
    assert not result.ok and not (external / "s.md").exists()


async def test_dry_run_never_enters_session_or_team_handlers(tmp_path, monkeypatch):
    executor = ToolExecutor(str(tmp_path))
    names_and_args = [
        ("enter_plan_mode", {}),
        ("goal", {"action": "create", "objective": "work"}),
        ("schedule_create", {"after_seconds": 5, "prompt": "later"}),
        ("team_task_create", {"title": "work"}),
    ]
    invoked = []
    for name, args in names_and_args:
        tool = executor.registry.get(name)
        monkeypatch.setattr(tool, "handler", lambda a, c: invoked.append(True))
        result = await executor.execute_tool_call("dry", call(name, args, name), {"dry_run": True})
        assert result.ok, result.error
        assert result.metadata["dry_run"]
    assert not invoked


async def test_planning_shell_cannot_mutate_even_when_claiming_read_effects(tmp_path):
    result = await ToolExecutor(str(tmp_path)).execute_tool_call(
        "plan-shell",
        call(
            "bash",
            {
                "command": "touch should-not-exist",
                "sideEffects": ["read-in-cwd"],
                "auto_background_on_timeout": False,
            },
        ),
        {"plan_mode": True, "sandbox_mode": "danger-full-access"},
    )
    assert not result.ok
    assert not (tmp_path / "should-not-exist").exists()


def test_approval_presents_complete_plan_and_denies_unpresentable_file(tmp_path):
    plan_file = tmp_path / ".coderai" / "plans" / "s.md"
    plan_file.parent.mkdir(parents=True)
    plan_file.write_text("# Actual plan\nChange the owned files")
    request = call("exit_plan_mode", {"summary": "Ready"})
    plan = compute_tool_call_permissions(
        session_id="s", project_root=str(tmp_path), tool_calls=[request]
    )
    assert plan["askPermissions"][0]["description"] == plan_file.read_text()
    plan_file.write_text("x" * 60_000)
    plan = compute_tool_call_permissions(
        session_id="s", project_root=str(tmp_path), tool_calls=[request]
    )
    assert plan["permissions"][0]["permission"] == "deny"
    assert not plan["askPermissions"]


def test_mcp_catalog_rejects_unresolved_schema_before_exposing_tool():
    from coderai.mcp.manager import McpManager, McpToolEntry

    manager = McpManager()
    client = MagicMock()
    manager.tools = [
        McpToolEntry(
            server_name="test",
            original_name="lookup",
            namespaced_name="mcp__test__lookup",
            definition={"inputSchema": {"$ref": "https://example.invalid/schema.json"}},
            client=client,
        )
    ]
    with pytest.raises(ValueError, match="mcp__test__lookup.*Remote schema reference"):
        manager.get_mcp_tool_definitions()
    client.call_tool.assert_not_called()
    manager.tools[0].definition["inputSchema"] = {
        "type": "object",
        "$defs": {"id": {"type": "integer", "minimum": 1}},
        "properties": {"id": {"$ref": "#/$defs/id"}},
    }
    assert manager.get_mcp_tool_definitions()[0]["function"]["parameters"]["$defs"]


async def test_early_validation_errors_redact_credentials_and_keep_error_metadata(tmp_path):
    secret = "sk-" + "x" * 48
    result = await ToolExecutor(str(tmp_path)).execute_tool_call(
        "s", call("goal", {"action": secret}), {}
    )
    assert not result.ok and secret not in result.error
    assert result.metadata["code"] == "INVALID_TOOL_ARGUMENTS"
    assert result.metadata["retryable"] is False
    malformed = await ToolExecutor(str(tmp_path)).execute_tool_call(
        "s", {"id": "bad", "function": {"name": "read", "arguments": "[1, 2]"}}, {}
    )
    assert malformed.metadata["code"] == "INVALID_TOOL_ARGUMENTS"
