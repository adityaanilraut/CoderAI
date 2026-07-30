"""Security boundaries for durable workspace transactions."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from coderAI.core.execution_context import create_run_context
from coderAI.core.services import services_scope
from coderAI.core.workspace_transactions import (
    TransactionState,
    WorkspaceTransactionError,
    WorkspaceTransactionStore,
)
from coderAI.system.config import Config


pytestmark = pytest.mark.security


def _context(workspace, ledger_root, session_id):
    store = WorkspaceTransactionStore(
        session_id=session_id,
        workspace_root=str(workspace),
        ledger_root=str(ledger_root),
    )
    return replace(
        create_run_context(workspace_root=str(workspace)),
        session_id=session_id,
        agent_id=f"agent_{session_id}",
        transaction_store=store,
    )


def _committed_change(context, target, value):
    store = context.transaction_store
    handle = store.begin(
        run_context=context,
        tool_call_id="call_security",
        tool_name="write_file",
        tool_arguments={"path": str(target)},
        objective="security regression",
        plan_id=None,
        plan_revision=None,
    )
    target.write_text(value)
    store.finalize(handle, run_context=context, tool_result={"success": True})
    return handle


def test_rollback_refuses_symlink_swap_and_preserves_outside_file(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "target.txt"
    target.write_text("before")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    context = _context(workspace, tmp_path / "ledgers", "session_symlink_tx")
    handle = _committed_change(context, target, "transaction")

    target.unlink()
    target.symlink_to(outside)
    with services_scope(config=Config(project_root=str(workspace))):
        result = context.transaction_store.rollback(
            handle.transaction_id,
            run_context=context,
        )

    assert result["success"] is False
    assert "refusing overwrite" in result["errors"][0]
    assert target.is_symlink()
    assert outside.read_text() == "outside"


def test_tampered_transaction_path_cannot_escape_workspace(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "target.txt"
    target.write_text("before")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    context = _context(workspace, tmp_path / "ledgers", "session_tamper_tx")
    handle = _committed_change(context, target, "transaction")
    record_path = context.transaction_store.store_dir / handle.transaction_id / "transaction.json"
    record = json.loads(record_path.read_text())
    record["changes"][0]["path"] = "../outside.txt"
    record_path.write_text(json.dumps(record))

    with services_scope(config=Config(project_root=str(workspace))):
        result = context.transaction_store.rollback(
            handle.transaction_id,
            run_context=context,
        )

    assert result["success"] is False
    assert "unsafe transaction path" in result["errors"][0]
    assert outside.read_text() == "outside"
    assert context.transaction_store.list_transactions()[0]["state"] == (
        TransactionState.PARTIAL_FAILURE.value
    )


def test_cross_session_and_cross_workspace_contexts_cannot_use_ledger(tmp_path) -> None:
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    first_workspace.mkdir()
    second_workspace.mkdir()
    target = first_workspace / "target.txt"
    target.write_text("before")
    owner = _context(first_workspace, tmp_path / "ledgers", "session_owner_tx")
    other_session = _context(first_workspace, tmp_path / "ledgers", "session_other_tx")
    other_workspace = _context(second_workspace, tmp_path / "ledgers", "session_workspace_tx")
    handle = _committed_change(owner, target, "transaction")

    with pytest.raises(WorkspaceTransactionError, match="does not own this session"):
        owner.transaction_store.rollback(handle.transaction_id, run_context=other_session)
    owner_session_wrong_workspace = replace(
        other_workspace,
        session_id=owner.session_id,
    )
    with pytest.raises(WorkspaceTransactionError, match="does not own this workspace"):
        owner.transaction_store.rollback(
            handle.transaction_id,
            run_context=owner_session_wrong_workspace,
        )
    assert target.read_text() == "transaction"
