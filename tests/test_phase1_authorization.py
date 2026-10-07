"""Exact-call sandbox grants and hook decisions through real dispatch/turns."""

from __future__ import annotations

from dataclasses import replace
import json
from unittest.mock import AsyncMock

import pytest

from coderai.hooks import MergedHookOutcome
from coderai.soul.approval import (
    PermissionTicketRegistry,
    apply_auto_approve_to_permission_plan,
    compute_tool_call_permissions,
    evaluate_permission_scopes,
    normalize_ask_permissions,
    resolve_tool_call_permission,
)
from coderai.soul.session.manager import SessionManager
from coderai.tools.legacy.authorization import (
    build_tool_authorizations,
    prepare_pre_tool_outcomes,
)
from coderai.tools.legacy.executor import ToolExecutor
from coderai.tools.legacy.registry import ToolRegistry
from coderai.tools.legacy.types import ToolExecutionContext, ToolExecutionHooks, ToolResult
from coderai.tools.shell import _effective_sandbox_mode


def _args(**overrides):
    return {
        "command": "printf harmless",
        "sideEffects": ["read-in-cwd"],
        "sandbox_permissions": "danger-full-access",
        "justification": "This is a model-written request, not approval.",
        **overrides,
    }


def _call(args, call_id="call-1", name="bash"):
    return {"id": call_id, "function": {"name": name, "arguments": json.dumps(args)}}


def _settings(mode):
    return {"sandbox": mode, "allow": ["read-in-cwd"], "defaultMode": "allowAll"}


@pytest.mark.parametrize("base", ["read-only", "workspace-write"])
def test_model_justification_cannot_disable_sandbox(tmp_path, base):
    context = ToolExecutionContext("s", str(tmp_path), sandbox_mode=base)
    mode, error = _effective_sandbox_mode(context, _args())
    assert mode == base
    assert error is not None and "explicit approval" in error.error


@pytest.mark.parametrize("base", ["read-only", "workspace-write"])
def test_escalation_cannot_be_auto_allowed_by_policy_yolo_or_ticket(tmp_path, base):
    registry = PermissionTicketRegistry()
    registry.request_escalation(session_id="s", tool_name="bash", scope="*")
    settings = _settings(base)
    settings["allow"].append("sandbox-escalation")
    plan = compute_tool_call_permissions(
        session_id="s",
        project_root=str(tmp_path),
        tool_calls=[_call(_args())],
        settings=settings,
        ticket_registry=registry,
    )
    assert plan["permissions"][0]["permission"] == "ask"
    assert "sandbox-escalation" in plan["askPermissions"][0]["scopes"]
    assert apply_auto_approve_to_permission_plan(plan)["askPermissions"] == plan["askPermissions"]


def _authorized_context(tmp_path):
    args = _args()
    call = _call(args)
    plan = compute_tool_call_permissions(
        session_id="s",
        project_root=str(tmp_path),
        tool_calls=[call],
        settings=_settings("read-only"),
    )
    grants = build_tool_authorizations(
        "s",
        str(tmp_path),
        [call],
        [{"toolCallId": call["id"], "permission": "allow"}],
        plan["askPermissions"],
        None,
    )
    return (
        args,
        call,
        ToolExecutionContext(
            "s",
            str(tmp_path),
            tool_call=call,
            sandbox_mode="read-only",
            authorization=grants[call["id"]],
        ),
    )


def test_explicit_grant_allows_only_the_approved_call(tmp_path):
    args, _, context = _authorized_context(tmp_path)
    assert _effective_sandbox_mode(context, args) == ("danger-full-access", None)


@pytest.mark.parametrize("change", ["command", "mode", "id", "session", "workspace", "isolation"])
def test_grant_cannot_be_replayed_in_changed_context(tmp_path, change):
    args, call, context = _authorized_context(tmp_path)
    if change == "command":
        args = {**args, "command": "printf altered"}
    elif change == "mode":
        args = {**args, "sandbox_permissions": "workspace-write"}
    elif change == "id":
        context = replace(context, tool_call={**call, "id": "another"})
    elif change == "session":
        context = replace(context, session_id="another")
    elif change == "workspace":
        context = replace(context, project_root=str(tmp_path / "other"))
    else:
        context = replace(context, isolated_cwd=str(tmp_path / "isolated"))
    _, error = _effective_sandbox_mode(context, args)
    assert error is not None and not error.ok


@pytest.mark.parametrize("reply", [None, "deny"])
def test_ordinary_allow_or_rejection_cannot_create_escalation_grant(tmp_path, reply):
    args = _args()
    call = _call(args)
    grants = build_tool_authorizations(
        "s",
        str(tmp_path),
        [call],
        [{"toolCallId": call["id"], "permission": reply}] if reply else None,
        [{"toolCallId": call["id"], "permission": "allow"}],
        None,
    )
    context = ToolExecutionContext(
        "s",
        str(tmp_path),
        tool_call=call,
        sandbox_mode="read-only",
        authorization=grants[call["id"]],
    )
    assert _effective_sandbox_mode(context, args)[1] is not None


def test_changing_args_before_building_grant_does_not_rebind_old_ask(tmp_path):
    args, _, context = _authorized_context(tmp_path)
    old_call = _call(args)
    plan = compute_tool_call_permissions(
        session_id="s",
        project_root=str(tmp_path),
        tool_calls=[old_call],
        settings=_settings("read-only"),
    )
    args = {**args, "command": "printf different"}
    call = _call(args)
    grants = build_tool_authorizations(
        "s",
        str(tmp_path),
        [call],
        [{"toolCallId": call["id"], "permission": "allow"}],
        plan["askPermissions"],
        None,
    )
    context = replace(context, tool_call=call, authorization=grants[call["id"]])
    assert _effective_sandbox_mode(context, args)[1] is not None


def test_existing_full_access_and_tightening_need_no_escalation(tmp_path):
    context = ToolExecutionContext("s", str(tmp_path), sandbox_mode="danger-full-access")
    assert _effective_sandbox_mode(context, _args()) == ("danger-full-access", None)
    assert _effective_sandbox_mode(context, _args(sandbox_permissions="read-only")) == (
        "read-only",
        None,
    )


def _counting_executor(tmp_path):
    seen = []
    registry = ToolRegistry()
    tool = registry.get("bash")

    def handler(args, context):
        mode, error = _effective_sandbox_mode(context, args)
        if error:
            return error
        seen.append(mode)
        return ToolResult(ok=True, name="bash", output="ran")

    tool.handler = handler
    return ToolExecutor(str(tmp_path), registry=registry), seen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        MergedHookOutcome(decision="ask"),
        MergedHookOutcome(stop=True),
        MergedHookOutcome(decision="deny"),
    ],
)
async def test_hook_ask_stop_and_deny_prevent_handler_dispatch(tmp_path, monkeypatch, outcome):
    monkeypatch.setattr("coderai.hooks.run_hook_point", lambda *a, **k: outcome)
    executor, seen = _counting_executor(tmp_path)
    args = _args(sandbox_permissions="workspace-write")
    result = await executor.execute_tool_call(
        "s", _call(args), ToolExecutionHooks(sandbox_mode="workspace-write")
    )
    assert not result.ok and seen == []
    if outcome.stop:
        assert result.concludes_turn


@pytest.mark.asyncio
async def test_hook_approval_is_independent_of_ordinary_permission(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "coderai.hooks.run_hook_point",
        lambda *a, **k: MergedHookOutcome(decision="ask", reason="review required"),
    )
    executor, seen = _counting_executor(tmp_path)
    args = _args(sandbox_permissions="workspace-write")
    call = _call(args)
    snapshots = prepare_pre_tool_outcomes("s", str(tmp_path), [call], {})
    plan = compute_tool_call_permissions(
        session_id="s",
        project_root=str(tmp_path),
        tool_calls=[call],
        settings=_settings("workspace-write"),
        pre_tool_outcomes=snapshots,
    )
    assert plan["askPermissions"][0]["scopes"] == ["hook-approval"]
    assert apply_auto_approve_to_permission_plan(plan)["askPermissions"]
    hooks = ToolExecutionHooks(sandbox_mode="workspace-write")
    hooks.authorizations = build_tool_authorizations(
        "s", str(tmp_path), [call], None, plan["askPermissions"], snapshots
    )
    assert not (await executor.execute_tool_call("s", call, hooks)).ok
    hooks.authorizations = build_tool_authorizations(
        "s",
        str(tmp_path),
        [call],
        [{"toolCallId": call["id"], "permission": "allow"}],
        plan["askPermissions"],
        snapshots,
    )
    assert (await executor.execute_tool_call("s", call, hooks)).ok
    assert seen == ["workspace-write"]


@pytest.mark.asyncio
async def test_hook_halt_prevents_following_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "coderai.hooks.run_hook_point", lambda *a, **k: MergedHookOutcome(stop=True)
    )
    executor, seen = _counting_executor(tmp_path)
    calls = [_call(_args(sandbox_permissions="workspace-write"), f"call-{n}") for n in range(2)]
    results = await executor.execute_tool_calls("s", calls)
    assert len(results) == 1 and seen == []
    assert results[0]["result"]["forceStopTurn"]


def _real_manager(tmp_path, monkeypatch, args, outcome):
    import coderai.hooks as hook_api

    monkeypatch.setattr("coderai.hooks.run_hook_point", lambda *a, **k: outcome)
    monkeypatch.setattr(
        hook_api,
        "run_hook_point_async",
        AsyncMock(side_effect=lambda *a, **k: hook_api.run_hook_point(*a, **k)),
    )
    manager = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": object(), "model": "gpt-4o"},
        get_resolved_settings=lambda: {
            "model": "gpt-4o",
            "permissions": _settings("workspace-write"),
        },
    )
    seen = []
    tool = manager.tool_executor.registry.get("bash")

    def handler(arguments, context):
        mode, error = _effective_sandbox_mode(context, arguments)
        if error:
            return error
        seen.append(mode)
        return ToolResult(ok=True, name="bash", output="approved action ran")

    monkeypatch.setattr(tool, "handler", handler)
    call = _call(args)
    manager._create_completion_with_retry = AsyncMock(
        side_effect=[
            {"choices": [{"message": {"content": "", "tool_calls": [call]}}]},
            {"choices": [{"message": {"content": "done"}}]},
        ]
    )
    return manager, seen


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["escalation", "hook"])
async def test_real_turn_pauses_even_in_yolo_and_resumes_exact_approval(
    tmp_path, monkeypatch, case, isolated_home
):
    args = _args() if case == "escalation" else _args(sandbox_permissions="workspace-write")
    outcome = (
        MergedHookOutcome(decision="ask", reason="review required")
        if case == "hook"
        else MergedHookOutcome()
    )
    manager, seen = _real_manager(tmp_path, monkeypatch, args, outcome)
    manager.set_yolo(True)
    try:
        sid = await manager.create_session("perform the harmless action", skills=[])
        entry = manager._get_entry(sid)
        assert entry["status"] == "ask_permission" and seen == []
        scope = "sandbox-escalation" if case == "escalation" else "hook-approval"
        assert scope in entry["askPermissions"][0]["scopes"]
        await manager.respond_permissions(sid, [{"toolCallId": "call-1", "permission": "allow"}])
        assert seen == ["danger-full-access" if case == "escalation" else "workspace-write"]
        assert manager._get_entry(sid)["status"] == "completed"
    finally:
        manager.dispose()


@pytest.mark.asyncio
async def test_real_turn_halts_without_running_tools_or_another_completion(
    tmp_path, monkeypatch, isolated_home
):
    manager, seen = _real_manager(
        tmp_path,
        monkeypatch,
        _args(sandbox_permissions="workspace-write"),
        MergedHookOutcome(stop=True, stop_reason="halt"),
    )
    try:
        sid = await manager.create_session("perform action", skills=[])
        assert seen == [] and manager._get_entry(sid)["status"] == "completed"
        assert manager._create_completion_with_retry.await_count == 1
        assert any(
            "PreToolUseStopped" in (m.content or "")
            for m in manager.list_session_messages(sid)
            if m.role == "tool"
        )
    finally:
        manager.dispose()


@pytest.mark.parametrize("change", ["tool", "session", "workspace"])
def test_saved_ask_cannot_be_rebound_to_another_identity(tmp_path, change):
    args = _args()
    call = _call(args)
    plan = compute_tool_call_permissions(
        session_id="s",
        project_root=str(tmp_path),
        tool_calls=[call],
        settings=_settings("read-only"),
    )
    session = "different" if change == "session" else "s"
    root = str(tmp_path / "other") if change == "workspace" else str(tmp_path)
    if change == "tool":
        plan["askPermissions"][0]["tool_name"] = "WebFetch"
    grant = build_tool_authorizations(
        session,
        root,
        [call],
        [{"toolCallId": call["id"], "permission": "allow"}],
        plan["askPermissions"],
        None,
    )[call["id"]]
    assert grant.sandbox_mode is None


def test_normalization_preserves_explicit_approval_binding(tmp_path):
    call = _call(_args())
    plan = compute_tool_call_permissions(
        session_id="s",
        project_root=str(tmp_path),
        tool_calls=[call],
        settings=_settings("read-only"),
    )
    normalized = normalize_ask_permissions(plan["askPermissions"])
    grant = build_tool_authorizations(
        "s",
        str(tmp_path),
        [call],
        [{"toolCallId": call["id"], "permission": "allow"}],
        normalized,
        None,
    )[call["id"]]
    assert grant.sandbox_mode == "danger-full-access"
    assert normalized[0]["requiresExplicitApproval"]


def test_yolo_and_explicit_reply_do_not_override_denied_sibling(tmp_path):
    denied = {"toolCallId": "denied", "permission": "deny"}
    plan = compute_tool_call_permissions(
        session_id="s",
        project_root=str(tmp_path),
        tool_calls=[_call(_args())],
        settings=_settings("read-only"),
    )
    plan["permissions"].append(denied)
    result = apply_auto_approve_to_permission_plan(plan)
    assert denied in result["permissions"]
    assert (
        resolve_tool_call_permission(
            "denied", [{"toolCallId": "denied", "permission": "allow"}], result["permissions"]
        )
        == "deny"
    )


def test_hook_halt_stops_preparation_of_later_hook_commands(tmp_path, monkeypatch):
    seen = []

    def hook(*a, **kw):
        seen.append(kw["payload"]["tool_input"]["command"])
        return MergedHookOutcome(stop=True)

    monkeypatch.setattr("coderai.hooks.run_hook_point", hook)
    calls = [_call(_args(command=str(n)), str(n)) for n in range(3)]
    snapshots = prepare_pre_tool_outcomes("s", str(tmp_path), calls, {})
    assert seen == ["0"] and list(snapshots) == ["0"]


def test_duplicate_ids_cannot_receive_grants_or_run_hooks(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr("coderai.hooks.run_hook_point", lambda *a, **k: seen.append(1))
    calls = [_call(_args()), _call(_args())]
    assert prepare_pre_tool_outcomes("s", str(tmp_path), calls, {}) == {} and seen == []
    plan = compute_tool_call_permissions(
        session_id="s",
        project_root=str(tmp_path),
        tool_calls=calls,
        settings=_settings("read-only"),
    )
    assert all(p["permission"] == "deny" for p in plan["permissions"])
    assert (
        build_tool_authorizations(
            "s",
            str(tmp_path),
            calls,
            [{"toolCallId": "call-1", "permission": "allow"}],
            plan["askPermissions"],
            None,
        )
        == {}
    )


@pytest.mark.asyncio
async def test_real_resume_preserves_hook_denied_sibling(tmp_path, monkeypatch, isolated_home):
    manager, seen = _real_manager(tmp_path, monkeypatch, _args(), MergedHookOutcome())

    def hook(*a, **kw):
        return (
            MergedHookOutcome(decision="deny")
            if kw["payload"]["tool_input"]["command"] == "denied"
            else MergedHookOutcome()
        )

    monkeypatch.setattr("coderai.hooks.run_hook_point", hook)
    calls = [
        _call(_args(command="denied", sandbox_permissions="workspace-write"), "denied"),
        _call(_args()),
    ]
    manager._create_completion_with_retry = AsyncMock(
        side_effect=[
            {"choices": [{"message": {"content": "", "tool_calls": calls}}]},
            {"choices": [{"message": {"content": "done"}}]},
        ]
    )
    manager.set_yolo(True)
    try:
        sid = await manager.create_session("perform actions", skills=[])
        assert manager._get_entry(sid)["status"] == "ask_permission" and seen == []
        await manager.respond_permissions(
            sid,
            [
                {"toolCallId": "call-1", "permission": "allow"},
                {"toolCallId": "denied", "permission": "allow"},
            ],
        )
        assert seen == ["danger-full-access"]
        assert any(
            "denied" in (m.content or "").lower()
            for m in manager.list_session_messages(sid)
            if m.role == "tool"
        )
    finally:
        manager.dispose()


def test_hook_input_change_is_denied_before_permission_planning(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "coderai.hooks.run_hook_point",
        lambda *a, **k: MergedHookOutcome(updated_input={"command": "a replacement"}),
    )
    call = _call(_args())
    snapshots = prepare_pre_tool_outcomes("s", str(tmp_path), [call], {})
    plan = compute_tool_call_permissions(
        session_id="s",
        project_root=str(tmp_path),
        tool_calls=[call],
        settings=_settings("read-only"),
        pre_tool_outcomes=snapshots,
    )
    assert plan["permissions"][0]["permission"] == "deny" and plan["askPermissions"] == []


@pytest.mark.asyncio
async def test_cached_hook_context_reaches_tool_follow_up(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "coderai.hooks.run_hook_point",
        lambda *a, **k: MergedHookOutcome(
            additional_context=["context"], system_messages=["system"]
        ),
    )
    executor, seen = _counting_executor(tmp_path)
    call = _call(_args(sandbox_permissions="workspace-write"))
    snapshots = prepare_pre_tool_outcomes("s", str(tmp_path), [call], {})
    hooks = ToolExecutionHooks(
        sandbox_mode="workspace-write",
        authorizations=build_tool_authorizations("s", str(tmp_path), [call], None, None, snapshots),
    )
    result = await executor.execute_tool_call("s", call, hooks)
    assert result.ok
    texts = [
        m.content if hasattr(m, "content") else m["content"] for m in result.follow_up_messages
    ]
    assert texts == ["context", "system"]


@pytest.mark.parametrize("scope", ["sandbox-escalation", "hook-approval"])
def test_explicit_scope_still_obeys_configured_deny(tmp_path, monkeypatch, scope):
    outcome = MergedHookOutcome(decision="ask") if scope == "hook-approval" else MergedHookOutcome()
    monkeypatch.setattr("coderai.hooks.run_hook_point", lambda *a, **k: outcome)
    args = _args(sandbox_permissions="workspace-write") if scope == "hook-approval" else _args()
    call = _call(args)
    settings = {**_settings("workspace-write"), "deny": [scope]}
    snapshots = prepare_pre_tool_outcomes("s", str(tmp_path), [call], {})
    plan = compute_tool_call_permissions(
        session_id="s",
        project_root=str(tmp_path),
        tool_calls=[call],
        settings=settings,
        pre_tool_outcomes=snapshots,
    )
    assert plan["permissions"][0]["permission"] == "deny" and plan["askPermissions"] == []


def test_unknown_scope_cannot_mask_configured_deny():
    assert (
        evaluate_permission_scopes(["unknown", "write-in-cwd"], {"deny": ["write-in-cwd"]})
        == "deny"
    )


@pytest.mark.asyncio
async def test_cached_hook_input_change_cannot_reach_handler(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "coderai.hooks.run_hook_point",
        lambda *a, **k: MergedHookOutcome(updated_input={"command": "replacement"}),
    )
    executor, seen = _counting_executor(tmp_path)
    call = _call(_args(sandbox_permissions="workspace-write"))
    snapshots = prepare_pre_tool_outcomes("s", str(tmp_path), [call], {})
    hooks = ToolExecutionHooks(
        sandbox_mode="workspace-write",
        authorizations=build_tool_authorizations("s", str(tmp_path), [call], None, None, snapshots),
    )
    result = await executor.execute_tool_call("s", call, hooks)
    assert not result.ok and seen == []
    assert "RequiresReplan" in result.error
