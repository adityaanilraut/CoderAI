"""Durable workspace transaction ledger and executor integration tests."""

from __future__ import annotations

import shlex
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coderAI.core.execution_context import (
    PermissionPolicySnapshot,
    create_run_context,
)
from coderAI.core.objective import ObjectiveState
from coderAI.core.services import services_scope
from coderAI.core.tool_executor import ToolExecutor
from coderAI.core.turn import TurnContext
from coderAI.core.workspace_transactions import (
    TransactionState,
    WorkspaceTransactionError,
    WorkspaceTransactionStore,
)
from coderAI.system.config import Config
from coderAI.tools import ToolRegistry
from coderAI.tools.filesystem import WriteFileTool
from coderAI.tools.terminal import RunCommandTool
from coderAI.tools.undo import FileBackupStore


def _bound_context(workspace, ledger_root, session_id: str = "session_tx"):
    context = create_run_context(
        workspace_root=str(workspace),
        permission_policy=PermissionPolicySnapshot(
            auto_approve=True,
            workspace_trusted=True,
            allowed_tools=frozenset({"run_command"}),
        ),
    )
    store = WorkspaceTransactionStore(
        session_id=session_id,
        workspace_root=str(workspace),
        ledger_root=str(ledger_root),
    )
    return replace(
        context,
        session_id=session_id,
        agent_id=f"agent_{session_id}",
        checkpoint_store=FileBackupStore(
            session_id=session_id,
            backup_root=str(ledger_root.parent / "backups"),
        ),
        transaction_store=store,
    )


def _begin(store, context, *, tool_name: str = "write_file"):
    return store.begin(
        run_context=context,
        tool_call_id="call_exact",
        tool_name=tool_name,
        tool_arguments={"path": "a.txt", "content": "redacted by hash"},
        objective="change the workspace safely",
        plan_id="plan_123",
        plan_revision=4,
    )


def _config(workspace) -> Config:
    return Config(project_root=str(workspace), save_history=False)


def test_open_recorded_committed_state_and_exact_context_links(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "a.txt"
    target.write_text("before")
    context = _bound_context(workspace, tmp_path / "ledgers")
    store = context.transaction_store

    handle = _begin(store, context)
    opened = store.list_transactions()[0]
    assert opened["state"] == TransactionState.OPEN.value

    target.write_text("after")
    committed = store.finalize(
        handle,
        run_context=context,
        tool_result={"success": True},
    )

    assert [item["state"] for item in committed["transitions"]] == [
        TransactionState.OPEN.value,
        TransactionState.RECORDED.value,
        TransactionState.COMMITTED.value,
    ]
    assert committed["run_id"] == context.run_id
    assert committed["session_id"] == context.session_id
    assert committed["agent_id"] == context.agent_id
    assert committed["workspace_id"] == context.workspace_id
    assert committed["tool_call_id"] == "call_exact"
    assert committed["objective"] == "change the workspace safely"
    assert committed["plan_execution"] == {"plan_id": "plan_123", "revision": 4}
    assert committed["changes"] == [
        {
            "path": "a.txt",
            "operation": "modified",
            "before": committed["changes"][0]["before"],
            "after": committed["changes"][0]["after"],
        }
    ]


def test_failed_tool_outcome_still_commits_observed_mutation(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = _bound_context(workspace, tmp_path / "ledgers")
    store = context.transaction_store
    handle = _begin(store, context)

    (workspace / "partial.txt").write_text("side effect before failure")
    record = store.finalize(
        handle,
        run_context=context,
        tool_result={"success": False, "error": "command exited 1"},
    )

    assert record["state"] == TransactionState.COMMITTED.value
    assert record["tool_success"] is False
    assert record["changes"][0]["path"] == "partial.txt"
    assert record["changes"][0]["operation"] == "created"


def test_incomplete_pre_snapshot_is_durable_partial_failure_and_blocks_open(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = _bound_context(workspace, tmp_path / "ledgers", "session_open_failure")
    store = context.transaction_store
    monkeypatch.setattr(
        store,
        "_scan_workspace",
        lambda **_kwargs: ({}, ["unreadable workspace entry"]),
    )

    with pytest.raises(WorkspaceTransactionError, match="could not open"):
        _begin(store, context)

    record = store.list_transactions()[0]
    assert record["state"] == TransactionState.PARTIAL_FAILURE.value
    assert record["rollback_ready"] is False
    assert "tool execution rejected" in record["transitions"][-1]["reason"]


def test_resume_recovers_open_transaction_and_can_roll_it_back(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "a.txt"
    target.write_text("before")
    original = _bound_context(workspace, tmp_path / "ledgers", "session_resume_tx")
    handle = _begin(original.transaction_store, original)
    target.write_text("after crash")

    resumed = replace(
        _bound_context(workspace, tmp_path / "ledgers", "session_resume_tx"),
        run_id="run_resumed",
    )
    recovered = resumed.transaction_store.recover_incomplete(run_context=resumed)

    assert recovered == [handle.transaction_id]
    record = resumed.transaction_store.list_transactions()[0]
    assert record["state"] == TransactionState.RECOVERED.value
    assert record["recovered_by_run_id"] == "run_resumed"

    with services_scope(config=_config(workspace)):
        result = resumed.transaction_store.rollback(
            handle.transaction_id,
            run_context=resumed,
        )
    assert result["success"] is True
    assert target.read_text() == "before"
    assert resumed.transaction_store.list_transactions()[0]["state"] == "rolled_back"


@pytest.mark.parametrize(
    ("interrupted_state", "recovered_state"),
    [
        (TransactionState.RECORDED.value, TransactionState.RECOVERED.value),
        (TransactionState.ROLLING_BACK.value, TransactionState.PARTIAL_FAILURE.value),
    ],
)
def test_resume_resolves_interrupted_commit_and_rollback_states(
    tmp_path, interrupted_state, recovered_state
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "a.txt"
    target.write_text("before")
    context = _bound_context(
        workspace,
        tmp_path / "ledgers",
        f"session_{interrupted_state}",
    )
    handle = _begin(context.transaction_store, context)
    target.write_text("after")
    context.transaction_store.finalize(
        handle,
        run_context=context,
        tool_result={"success": True},
    )
    record_path = context.transaction_store.store_dir / handle.transaction_id / "transaction.json"
    record = json.loads(record_path.read_text())
    record["state"] = interrupted_state
    record_path.write_text(json.dumps(record))

    recovered = context.transaction_store.recover_incomplete(run_context=context)

    assert recovered == [handle.transaction_id]
    assert context.transaction_store.list_transactions()[0]["state"] == recovered_state


def test_rollback_restores_modify_delete_and_create_deterministically(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    modified = workspace / "modified.txt"
    deleted = workspace / "deleted.txt"
    modified.write_text("old modified")
    deleted.write_text("old deleted")
    context = _bound_context(workspace, tmp_path / "ledgers")
    store = context.transaction_store
    handle = _begin(store, context)

    modified.write_text("new modified")
    deleted.unlink()
    created_dir = workspace / "new-dir"
    created_dir.mkdir()
    (created_dir / "created.txt").write_text("new")
    store.finalize(handle, run_context=context, tool_result={"success": True})

    with services_scope(config=_config(workspace)):
        result = store.rollback(handle.transaction_id, run_context=context)

    assert result["success"] is True
    assert modified.read_text() == "old modified"
    assert deleted.read_text() == "old deleted"
    assert not created_dir.exists()
    rolled_back = store.list_transactions()[0]
    assert rolled_back["state"] == TransactionState.ROLLED_BACK.value
    assert [item["state"] for item in rolled_back["transitions"]][-2:] == [
        TransactionState.ROLLING_BACK.value,
        TransactionState.ROLLED_BACK.value,
    ]


def test_rollback_conflict_is_partial_and_preserves_later_change(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "a.txt"
    target.write_text("before")
    context = _bound_context(workspace, tmp_path / "ledgers")
    store = context.transaction_store
    handle = _begin(store, context)
    target.write_text("transaction value")
    store.finalize(handle, run_context=context, tool_result={"success": True})
    target.write_text("user value after transaction")

    with services_scope(config=_config(workspace)):
        result = store.rollback(handle.transaction_id, run_context=context)

    assert result["success"] is False
    assert "refusing overwrite" in result["errors"][0]
    assert target.read_text() == "user value after transaction"
    assert store.list_transactions()[0]["state"] == TransactionState.PARTIAL_FAILURE.value


def test_parent_context_cannot_read_or_rollback_child_ledger(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parent = _bound_context(workspace, tmp_path / "ledgers", "session_parent_tx")
    child = _bound_context(workspace, tmp_path / "ledgers", "session_child_tx")
    handle = _begin(child.transaction_store, child)
    (workspace / "child.txt").write_text("child")
    child.transaction_store.finalize(
        handle,
        run_context=child,
        tool_result={"success": True},
    )

    assert parent.transaction_store.list_transactions() == []
    with pytest.raises(WorkspaceTransactionError, match="does not own"):
        child.transaction_store.rollback(handle.transaction_id, run_context=parent)
    assert (workspace / "child.txt").read_text() == "child"


@pytest.mark.asyncio
async def test_executor_records_real_shell_observed_changes(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = _bound_context(workspace, tmp_path / "ledgers", "session_shell_tx")
    config = _config(workspace)
    registry = ToolRegistry()
    registry.register(RunCommandTool())
    agent = SimpleNamespace(
        run_context=context,
        tracker_info=None,
        tools=registry,
        config=config,
        auto_approve=True,
        plan_mode=False,
        active_plan_id=None,
        active_plan_revision=None,
        _tool_approval_allowlist=None,
    )
    executor = ToolExecutor(agent)
    executor._turn = TurnContext(objective_state=ObjectiveState("create from shell"))
    hooks = SimpleNamespace(run_hooks=AsyncMock(return_value=[]))
    target = workspace / "shell.txt"
    command = f"printf shell-value > {shlex.quote(str(target))}"

    with services_scope(config=config):
        result = await executor.execute_single_tool(
            {
                "tool_id": "call_shell",
                "tool_name": "run_command",
                "arguments": {"command": command, "working_dir": str(workspace)},
                "parse_error": None,
            },
            {},
            hooks,
        )

    assert result["success"] is True
    assert result["_transaction_state"] == TransactionState.COMMITTED.value
    assert result["_workspace_changes"] == [{"path": "shell.txt", "operation": "created"}]
    record = context.transaction_store.list_transactions()[0]
    assert record["tool_name"] == "run_command"
    assert record["tool_call_id"] == "call_shell"


@pytest.mark.asyncio
async def test_executor_records_native_filesystem_mutation(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = _bound_context(workspace, tmp_path / "ledgers", "session_native_tx")
    config = _config(workspace)
    registry = ToolRegistry()
    registry.register(WriteFileTool())
    agent = SimpleNamespace(
        run_context=context,
        tracker_info=None,
        tools=registry,
        config=config,
        auto_approve=True,
        plan_mode=False,
        active_plan_id=None,
        active_plan_revision=None,
        _tool_approval_allowlist=None,
    )
    executor = ToolExecutor(agent)
    hooks = SimpleNamespace(run_hooks=AsyncMock(return_value=[]))

    with services_scope(config=config):
        result = await executor.execute_single_tool(
            {
                "tool_id": "call_native",
                "tool_name": "write_file",
                "arguments": {
                    "path": str(workspace / "native.txt"),
                    "content": "native-value",
                },
                "parse_error": None,
            },
            {},
            hooks,
        )

    assert result["success"] is True
    assert result["_workspace_changes"] == [{"path": "native.txt", "operation": "created"}]
    assert context.transaction_store.list_transactions()[0]["tool_call_id"] == "call_native"


@pytest.mark.asyncio
async def test_denied_and_plan_blocked_calls_never_open_transactions(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = _bound_context(workspace, tmp_path / "ledgers", "session_denied_tx")
    config = _config(workspace)
    registry = ToolRegistry()
    registry.register(RunCommandTool())
    agent = SimpleNamespace(
        run_context=context,
        tracker_info=None,
        tools=registry,
        config=config,
        auto_approve=False,
        confirmation_override=AsyncMock(return_value=False),
        plan_mode=False,
        active_plan_id=None,
        active_plan_revision=None,
        _tool_approval_allowlist=None,
    )
    executor = ToolExecutor(agent)
    hooks = SimpleNamespace(run_hooks=AsyncMock(return_value=[]))
    call = {
        "tool_id": "call_denied",
        "tool_name": "run_command",
        "arguments": {"command": "touch denied.txt", "working_dir": str(workspace)},
        "parse_error": None,
    }

    with services_scope(config=config):
        denied = await executor.execute_single_tool(call, {}, hooks)
    assert denied["success"] is False
    assert context.transaction_store.list_transactions() == []
    assert not (workspace / "denied.txt").exists()

    agent.auto_approve = True
    agent.plan_mode = True
    with services_scope(config=config):
        plan_blocked = await executor.execute_single_tool(call, {}, hooks)
    assert plan_blocked["success"] is False
    assert "Plan Mode" in plan_blocked["error"]
    assert context.transaction_store.list_transactions() == []


def test_transaction_store_rejects_unsafe_ids_and_symlink_escape(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(ValueError, match="path-safe"):
        WorkspaceTransactionStore(
            session_id="../escape",
            workspace_root=str(workspace),
            ledger_root=str(tmp_path / "ledgers"),
        )

    ledger_root = tmp_path / "ledgers"
    outside = tmp_path / "outside"
    ledger_root.mkdir()
    outside.mkdir()
    (ledger_root / "session_escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes ledger_root"):
        WorkspaceTransactionStore(
            session_id="session_escape",
            workspace_root=str(workspace),
            ledger_root=str(ledger_root),
        )
