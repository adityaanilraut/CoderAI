"""Session grants agree across public adapters and never widen exact-call grants."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from coderai.acp.engine import SessionManagerEngine
from coderai.approval_runtime.session_grants import SessionApprovalStore
from coderai.soul.approval import compute_tool_call_permissions
from coderai.soul.session.manager import SessionManager
from coderai.wire.types import ApprovalRequest


def call(call_id="one", path="missing", name="read", **args):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps({"file_path": path, **args})},
    }


def plan(root, calls, store, **kwargs):
    return compute_tool_call_permissions(
        session_id=kwargs.pop("session_id", "session"),
        project_root=str(root),
        tool_calls=calls,
        session_approval_store=store,
        **kwargs,
    )


@pytest.mark.parametrize(
    "mismatch", ["arguments", "tool", "session", "root", "scopes", "deny", "plan"]
)
def test_session_grants_only_match_intended_action(tmp_path, mismatch):
    store = SessionApprovalStore()
    original = plan(tmp_path, [call()], store)["askPermissions"][0]
    assert store.grant(original)
    assert not plan(tmp_path, [call("second")], store)["askPermissions"]
    args = {}
    target = call("changed")
    root = tmp_path
    if mismatch == "arguments":
        target = call("changed", "other")
    if mismatch == "tool":
        target = call("changed", name="write", content="hi")
    if mismatch == "session":
        args["session_id"] = "other-session"
    if mismatch == "root":
        root = tmp_path / "other-root"
    if mismatch == "scopes":
        target = call("changed", "../outside")
    if mismatch == "deny":
        args["settings"] = {"deny": ["read-in-cwd"]}
    if mismatch == "plan":
        args["force_ask_scopes"] = ["read-in-cwd"]
    outcome = plan(root, [target], store, **args)
    assert outcome["permissions"][0]["permission"] in {"ask", "deny"}


@pytest.mark.parametrize("special", ["sandbox-escalation", "hook-approval"])
def test_exact_invocation_approvals_cannot_be_cached(tmp_path, special):
    store = SessionApprovalStore()
    args = (
        {"command": "true", "sandbox_permissions": "danger-full-access", "justification": "need"}
        if special == "sandbox-escalation"
        else {"file_path": "missing"}
    )
    c = {
        "id": "one",
        "type": "function",
        "function": {
            "name": "bash" if special == "sandbox-escalation" else "read",
            "arguments": json.dumps(args),
        },
    }
    kwargs = {"settings": {"defaultMode": "allowAll", "sandbox": "read-only"}}
    if special == "hook-approval":
        from coderai.tools.legacy.types import tool_call_binding

        kwargs["pre_tool_outcomes"] = {
            "one": {
                **tool_call_binding("session", str(tmp_path), c),
                "outcome": {"decision": "ask"},
            }
        }
    request = plan(tmp_path, [c], store, **kwargs)["askPermissions"][0]
    assert special in request["scopes"]
    assert not store.grant(request)
    assert plan(tmp_path, [c], store, **kwargs)["askPermissions"]


def make_manager(root):
    responses = iter(
        [
            {"choices": [{"message": {"tool_calls": [call("one")]}}]},
            {"choices": [{"message": {"tool_calls": [call("two")]}}]},
            {"choices": [{"message": {"tool_calls": [call("three", "other")]}}]},
            {"choices": [{"message": {"content": "done"}}]},
        ]
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: next(responses)))
    )
    return SessionManager(
        project_root=str(root),
        create_openai_client=lambda: {"client": client, "model": "gpt-4o"},
        get_resolved_settings=lambda: {
            "model": "gpt-4o",
            "mergeAllAvailableSkills": False,
            "permissions": {"defaultMode": "askAll"},
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["engine", "wire", "sdk", "cli", "acp"])
async def test_real_manager_adapter_reuses_only_matching_session_grant(
    tmp_path, monkeypatch, adapter
):
    mgr = make_manager(tmp_path)
    approved = []
    try:
        if adapter == "wire":
            from coderai.wire.server import WireServer

            server = WireServer(mgr)
            server._initialized = True

            async def send(frame):
                if frame.get("method") == "request" and frame["id"] in server._pending:
                    request = server._pending[frame["id"]]
                    if isinstance(request, ApprovalRequest):
                        approved.append(request.tool_call_id)
                        request.resolve("approve_for_session")

            monkeypatch.setattr(server, "_send", send)
            await server._handle_prompt("prompt", {"user_input": "hi"})
            sid = server._session_id
        elif adapter == "cli":
            import coderai.ui.shell.app as app

            def prompt(items, *args, **kwargs):
                approved.extend(item["toolCallId"] for item in items)
                return [
                    {
                        "toolCallId": item["toolCallId"],
                        "permission": "allow",
                        "decision": "approve_for_session",
                    }
                    for item in items
                ], []

            monkeypatch.setattr(app, "_prompt_permissions", prompt)
            sid = await mgr.create_session("hi")
            await app._drain_pending_interactions(mgr, sid, False)
        elif adapter == "acp":
            import acp
            from coderai.acp.session import ACPSession

            async def update(**kwargs):
                pass

            async def permission(*args, **kwargs):
                approved.append("request")
                return acp.schema.RequestPermissionResponse(
                    outcome=acp.schema.AllowedOutcome(
                        outcome="selected", option_id="approve_for_session"
                    )
                )

            engine = SessionManagerEngine(mgr)
            session = ACPSession(
                "acp", engine, SimpleNamespace(session_update=update, request_permission=permission)
            )
            await session.prompt([acp.schema.TextContentBlock(type="text", text="hi")])
            sid = engine.session_id
        elif adapter == "sdk":
            monkeypatch.syspath_prepend(
                str(Path(__file__).resolve().parents[1] / "sdks/coderai-sdk/src")
            )
            from coderai_sdk import CoderAIClient

            def policy(request):
                approved.append(request.tool_call_id)
                return "approve_for_session"

            engine = SessionManagerEngine(mgr)
            sdk = CoderAIClient(engine=engine, permission_policy=policy)
            result = await sdk.prompt("hi")
            assert result.text == "done"
            sid = engine.session_id
        else:
            from kosong.message import TextPart

            engine = SessionManagerEngine(mgr)
            async for event in engine.run([TextPart(text="hi")], asyncio.Event()):
                if isinstance(event, ApprovalRequest):
                    approved.append(event.tool_call_id)
                    event.resolve("approve_for_session")
            sid = engine.session_id
        assert len(approved) == 2
        assert mgr.get_session(sid).status == "completed"
        store = mgr.session_approval_store
        assert store._grants
        assert mgr.delete_session(sid)
        assert not store._grants
    finally:
        mgr.dispose()


@pytest.mark.parametrize("scope", ["write-in-cwd", "unknown"])
def test_cli_session_selection_does_not_persist_project_permissions(monkeypatch, scope):
    import coderai.ui.shell.app as app

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda _: "a")
    replies, persistent = app._prompt_permissions(
        [{"toolCallId": "one", "name": "write", "command": "write x", "scopes": [scope]}],
        False,
    )
    assert replies == [
        {"toolCallId": "one", "permission": "allow", "decision": "approve_for_session"}
    ]
    assert persistent == []


@pytest.mark.asyncio
async def test_runtime_approval_uses_production_response_and_scoped_reuse(monkeypatch):
    from coderai.soul.approval import Approval
    from coderai.soul.tool_context import set_current_tool_call, set_session_id
    from coderai.wire.types import ToolCall

    approval = Approval()
    monkeypatch.setattr("coderai.soul.get_wire_or_none", lambda: None)
    seen = []
    approval.runtime.subscribe(
        lambda event: (
            (
                seen.append(event.request),
                approval.runtime.resolve(event.request.id, "approve_for_session"),
            )
            if event.kind == "request_created"
            else None
        )
    )
    set_session_id("owner")
    token = set_current_tool_call(ToolCall.model_validate(call()))
    try:
        assert await approval.request("read", "read", "read missing")
        assert await approval.request("read", "read", "read missing")
        assert len(seen) == 1
        assert await approval.request("read", "read", "changed description")
        assert len(seen) == 2
        set_session_id("other")
        assert await approval.request("read", "read", "read missing")
        assert len(seen) == 3
        approval.runtime.clear_session_grants()
        assert not approval.runtime._session_grants
    finally:
        from coderai.soul.tool_context import current_tool_call

        current_tool_call.reset(token)
        set_session_id("")


def test_grants_are_ephemeral_and_deleting_one_owner_retains_sibling(tmp_path):
    store = SessionApprovalStore()
    requests = [
        plan(tmp_path, [call()], store, session_id=owner)["askPermissions"][0]
        for owner in ("a", "b")
    ]
    for request in requests:
        assert store.grant(request)
    store.clear("a")
    assert plan(tmp_path, [call("next")], store, session_id="a")["askPermissions"]
    assert not plan(tmp_path, [call("next")], store, session_id="b")["askPermissions"]
    assert plan(tmp_path, [call("next")], SessionApprovalStore(), session_id="b")["askPermissions"]
    store.clear()
    assert not store._grants


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid", ["changed-call", "duplicate-reply", "stale-status", "plan-mode", "missing-binding"]
)
async def test_manager_rejects_unbound_or_stale_session_grant(tmp_path, monkeypatch, invalid):
    mgr = make_manager(tmp_path)
    try:
        sid = await mgr.create_session("hi")
        entry = mgr._get_entry(sid)
        request = entry["askPermissions"][0]
        reply = {"toolCallId": "one", "permission": "allow", "decision": "approve_for_session"}
        replies = [reply]
        if invalid == "changed-call":
            entry["toolCalls"] = [call("one", "changed")]
        if invalid == "duplicate-reply":
            replies.append(dict(reply))
        if invalid == "stale-status":
            entry["status"] = "completed"
        if invalid == "plan-mode":
            entry["planMode"] = True
        if invalid == "missing-binding":
            request.pop("args_digest")
        monkeypatch.setattr(mgr, "_get_entry", lambda _: entry)
        mgr._record_session_approval_replies(sid, entry, replies)
        assert not mgr.session_approval_store._grants
    finally:
        mgr.dispose()


def test_closed_runtime_rejects_late_session_decision_and_retains_siblings():
    from coderai.approval_runtime import ApprovalRuntime
    from coderai.approval_runtime.models import ApprovalSource

    runtime = ApprovalRuntime()
    requests = [
        runtime.create_request(
            tool_call_id=owner,
            action="read",
            description="read missing",
            source=ApprovalSource(kind="foreground_turn", id=owner),
            session_grant_key="action",
        )
        for owner in ("a", "b")
    ]
    runtime.clear_session_grants("a")
    runtime.resolve(requests[0].id, "approve_for_session")
    assert requests[0].status == "cancelled"
    assert not runtime._session_grants
    runtime.resolve(requests[1].id, "approve_for_session")
    assert runtime._session_grants == {("b", "action")}
    late = runtime.create_request(
        tool_call_id="late",
        action="read",
        description="read missing",
        source=ApprovalSource(kind="foreground_turn", id="a"),
        session_grant_key="action",
    )
    assert late.status == "cancelled"
    runtime.clear_session_grants()
    assert not runtime._session_grants


def test_closing_runtime_cannot_restore_grant_via_reentrant_subscriber():
    from coderai.approval_runtime import ApprovalRuntime
    from coderai.approval_runtime.models import ApprovalSource

    runtime = ApprovalRuntime()
    records = [
        runtime.create_request(
            tool_call_id=str(i),
            action="read",
            description="read missing",
            source=ApprovalSource(kind="foreground_turn", id="a"),
            session_grant_key="action",
        )
        for i in range(2)
    ]
    runtime.subscribe(
        lambda event: (
            runtime.resolve(records[1].id, "approve_for_session")
            if event.kind == "request_resolved" and event.request.id == records[0].id
            else None
        )
    )
    runtime.clear_session_grants("a")
    assert all(record.status == "cancelled" for record in records)
    assert not runtime._session_grants


@pytest.mark.asyncio
async def test_closed_runtime_direct_approval_returns_denied_without_wire_prompt(monkeypatch):
    from coderai.soul.approval import Approval
    from coderai.soul.tool_context import set_session_id

    approval = Approval()
    approval.runtime.clear_session_grants("owner")
    sent = []
    monkeypatch.setattr(
        "coderai.soul.get_wire_or_none",
        lambda: SimpleNamespace(soul_side=SimpleNamespace(send=sent.append)),
    )
    set_session_id("owner")
    try:
        assert not await approval.request("read", "read", "read missing")
        assert not sent
    finally:
        set_session_id("")
