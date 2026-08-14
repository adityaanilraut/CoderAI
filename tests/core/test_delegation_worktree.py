"""Milestone 3 isolated delegation worktree and patch integration tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coderAI.core.delegation_worktree import (
    DelegationWorktree,
    DelegationWorktreeError,
)
from coderAI.core.execution_context import create_run_context
from coderAI.core.tool_executor import ToolExecutor
from coderAI.system.history import Session
from coderAI.core.workspace_transactions import WorkspaceTransactionStore
from coderAI.tools.subagent import DelegateTaskTool, SubagentContext


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "CoderAI Tests")
    (root / "tracked.txt").write_text("committed\n")
    (root / "unchanged.txt").write_text("stable\n")
    _git(root, "add", "tracked.txt", "unchanged.txt")
    _git(root, "commit", "-qm", "base")
    return root


def test_worktree_seeds_dirty_and_untracked_parent_then_integrates_only_child_delta(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    (root / "tracked.txt").write_text("parent dirty baseline\n")
    (root / "untracked.txt").write_text("parent untracked baseline\n")
    manager = DelegationWorktree(
        parent_root=root,
        storage_root=tmp_path / "worktrees",
        worktree_id="delegation_seed",
    )
    child = manager.create()
    try:
        assert (child / "tracked.txt").read_text() == "parent dirty baseline\n"
        assert (child / "untracked.txt").read_text() == "parent untracked baseline\n"

        (child / "tracked.txt").write_text("child edit\n")
        (child / "new.py").write_text("VALUE = 1\n")
        prepared = manager.prepare_patch()

        assert [change.path for change in prepared.changes] == ["new.py", "tracked.txt"]
        assert "a/tracked.txt" in prepared.preview
        assert (root / "tracked.txt").read_text() == "parent dirty baseline\n"
        applied = manager.integrate(prepared)

        assert applied == [
            {"path": "new.py", "operation": "added"},
            {"path": "tracked.txt", "operation": "modified"},
        ]
        assert (root / "tracked.txt").read_text() == "child edit\n"
        assert (root / "new.py").read_text() == "VALUE = 1\n"
        assert (root / "untracked.txt").read_text() == "parent untracked baseline\n"
    finally:
        manager.cleanup()


def test_parent_drift_conflicts_without_overwrite(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manager = DelegationWorktree(
        parent_root=root,
        storage_root=tmp_path / "worktrees",
        worktree_id="delegation_conflict",
    )
    child = manager.create()
    try:
        (child / "tracked.txt").write_text("child edit\n")
        prepared = manager.prepare_patch()
        (root / "tracked.txt").write_text("concurrent parent edit\n")

        with pytest.raises(DelegationWorktreeError, match="parent workspace changed"):
            manager.integrate(prepared)
        assert (root / "tracked.txt").read_text() == "concurrent parent edit\n"
    finally:
        manager.cleanup()


def test_child_drift_after_review_is_rejected(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manager = DelegationWorktree(
        parent_root=root,
        storage_root=tmp_path / "worktrees",
        worktree_id="delegation_child_drift",
    )
    child = manager.create()
    try:
        (child / "tracked.txt").write_text("reviewed\n")
        prepared = manager.prepare_patch()
        (child / "tracked.txt").write_text("changed after review\n")

        with pytest.raises(DelegationWorktreeError, match="changed after patch review"):
            manager.integrate(prepared)
        assert (root / "tracked.txt").read_text() == "committed\n"
    finally:
        manager.cleanup()


def test_changed_symlink_fails_closed(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    root = _repo(tmp_path)
    manager = DelegationWorktree(
        parent_root=root,
        storage_root=tmp_path / "worktrees",
        worktree_id="delegation_symlink",
    )
    child = manager.create()
    try:
        os.symlink("tracked.txt", child / "linked.txt")
        with pytest.raises(DelegationWorktreeError, match="symlink"):
            manager.prepare_patch()
        assert not (root / "linked.txt").exists()
    finally:
        manager.cleanup()


def test_internal_task_state_is_not_part_of_integrated_patch(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manager = DelegationWorktree(
        parent_root=root,
        storage_root=tmp_path / "worktrees",
        worktree_id="delegation_internal",
    )
    child = manager.create()
    try:
        tasks = child / ".coderAI" / "tasks.json"
        tasks.parent.mkdir()
        tasks.write_text('{"child": true}')
        prepared = manager.prepare_patch()
        assert prepared.changes == ()
    finally:
        manager.cleanup()


def test_cleanup_unregisters_and_removes_generated_worktree(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manager = DelegationWorktree(
        parent_root=root,
        storage_root=tmp_path / "worktrees",
        worktree_id="delegation_cleanup",
    )
    child = manager.create()
    assert str(child) in _git(root, "worktree", "list", "--porcelain")

    manager.cleanup()

    assert not manager.allocation_root.exists()
    assert str(child) not in _git(root, "worktree", "list", "--porcelain")


def test_non_git_workspace_is_rejected_without_allocating(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    manager = DelegationWorktree(
        parent_root=root,
        storage_root=tmp_path / "worktrees",
        worktree_id="delegation_plain",
    )

    with pytest.raises(DelegationWorktreeError):
        manager.create()
    assert not manager.allocation_root.exists()


def test_delegate_rejects_parent_run_context_for_a_different_workspace(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    tool = DelegateTaskTool()
    tool.context = SubagentContext(
        parent_config=SimpleNamespace(project_root=str(root), budget_limit=0.0),
        parent_run_context=create_run_context(workspace_root=str(other)),
        parent_patch_approval=AsyncMock(return_value=True),
        workspace_isolation=True,
    )

    result = asyncio.run(tool.execute(task_description="edit tracked.txt"))

    assert result["success"] is False
    assert "does not match" in result["error"]
    assert (root / "tracked.txt").read_text() == "committed\n"


def _delegated_child(workspace_holder: dict[str, Path]) -> MagicMock:
    child = MagicMock()
    child.model = "test-model"
    child.config = SimpleNamespace(budget_limit=0.0)
    child.total_tokens = 0
    child.total_prompt_tokens = 0
    child.total_completion_tokens = 0
    child.provider = SimpleNamespace(actual_model="test-model")
    child.session = Session(session_id="session_child_12345678", model="test-model")
    child.tracker_info = None
    child.approval_port = None
    child.context_controller = MagicMock()
    child.context_controller.project_instructions = None
    child.context_controller._instructions_loaded = True
    child.cost_tracker = MagicMock()
    child.tools = SimpleNamespace(
        tools={
            "read_file": SimpleNamespace(is_read_only=True),
            "write_file": SimpleNamespace(is_read_only=False),
            "run_background": SimpleNamespace(is_read_only=False),
            "git_commit": SimpleNamespace(is_read_only=False),
        }
    )
    child.tools.get = lambda name: child.tools.tools.get(name)
    child.set_persona = MagicMock(return_value=None)
    child._bind_isolation_domain = MagicMock()
    child._configure_delegate_tool_context = MagicMock()
    child._register_tracker = MagicMock()
    child.create_session = MagicMock(return_value=child.session)
    child.save_session = MagicMock()
    child.close = AsyncMock()

    async def execute_task(_description: str, progress_callback=None) -> str:
        del progress_callback
        workspace = workspace_holder["root"]
        assert workspace != workspace_holder["parent"]
        (workspace / "tracked.txt").write_text("isolated child edit\n")
        return "Summary: edited tracked.txt"

    child.process_single_shot = AsyncMock(side_effect=execute_task)
    return child


def test_delegate_task_runs_in_isolated_root_and_integrates_after_approval(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    holder = {"parent": root}
    child = _delegated_child(holder)
    approval = AsyncMock(return_value=True)
    tool = DelegateTaskTool()
    tool.context = SubagentContext(
        parent_config=SimpleNamespace(project_root=str(root), budget_limit=0.0),
        parent_run_context=create_run_context(workspace_root=str(root)),
        parent_patch_approval=approval,
        workspace_isolation=True,
    )

    def build_agent(**kwargs):
        holder["root"] = Path(kwargs["project_root"])
        return child

    with patch("coderAI.core.agent.Agent", side_effect=build_agent) as agent_class:
        result = asyncio.run(tool.execute(task_description="edit tracked.txt"))

    assert result["success"] is True
    assert result["patch_status"] == "integrated"
    assert result["_workspace_changes"] == [{"path": "tracked.txt", "operation": "modified"}]
    assert (root / "tracked.txt").read_text() == "isolated child edit\n"
    assert not holder["root"].exists()
    assert agent_class.call_args.kwargs["workspace_trusted"] is False
    assert "run_background" not in child.tools.tools
    assert "git_commit" not in child.tools.tools
    approval.assert_awaited_once()
    assert "a/tracked.txt" in approval.await_args.args[1]


def test_delegate_patch_denial_leaves_parent_unchanged(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    holder = {"parent": root}
    child = _delegated_child(holder)
    tool = DelegateTaskTool()
    tool.context = SubagentContext(
        parent_config=SimpleNamespace(project_root=str(root), budget_limit=0.0),
        parent_run_context=create_run_context(workspace_root=str(root)),
        parent_patch_approval=AsyncMock(return_value=False),
        workspace_isolation=True,
    )

    def build_agent(**kwargs):
        holder["root"] = Path(kwargs["project_root"])
        return child

    with patch("coderAI.core.agent.Agent", side_effect=build_agent):
        result = asyncio.run(tool.execute(task_description="edit tracked.txt"))

    assert result["success"] is False
    assert result["patch_status"] == "denied"
    assert (root / "tracked.txt").read_text() == "committed\n"
    assert not holder["root"].exists()


def test_approved_patch_integration_is_recorded_in_parent_transaction_ledger(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    manager = DelegationWorktree(
        parent_root=root,
        storage_root=tmp_path / "worktrees",
        worktree_id="delegation_transaction",
    )
    child = manager.create()
    try:
        (child / "tracked.txt").write_text("transactional child edit\n")
        prepared = manager.prepare_patch()
        context = create_run_context(workspace_root=str(root))
        store = WorkspaceTransactionStore(
            session_id="session_parent_12345678",
            workspace_root=str(root),
            ledger_root=str(tmp_path / "transactions"),
        )
        context = replace(
            context,
            session_id="session_parent_12345678",
            transaction_store=store,
        )
        delegate = SimpleNamespace(
            is_read_only=False,
            safe=True,
            requires_confirmation=False,
            retryable=False,
            timeout=30.0,
        )

        async def integrate(_name, **_arguments):
            changes = manager.integrate(prepared)
            return {
                "success": True,
                "patch_status": "integrated",
                "_workspace_changes": changes,
            }

        registry = SimpleNamespace(
            get=lambda _name: delegate,
            execute=AsyncMock(side_effect=integrate),
        )
        agent = SimpleNamespace(
            run_context=context,
            tracker_info=None,
            tools=registry,
            auto_approve=True,
            approval_port=None,
            config=SimpleNamespace(project_root=str(root)),
            active_plan_id=None,
            active_plan_revision=None,
        )
        executor = ToolExecutor(agent)
        result = asyncio.run(
            executor.execute_single_tool(
                {
                    "tool_id": "delegate-call",
                    "tool_name": "delegate_task",
                    "arguments": {
                        "task_description": "edit tracked.txt",
                        "isolation_domain": "workspace",
                    },
                    "parse_error": None,
                },
                hooks_data=None,
                hooks_manager=SimpleNamespace(run_hooks=AsyncMock(return_value=[])),
            )
        )

        assert result["success"] is True, repr(result)
        assert result["patch_status"] == "integrated"
        assert result["_transaction_state"] == "committed"
        assert (root / "tracked.txt").read_text() == "transactional child edit\n"
        records = store.list_transactions()
        assert len(records) == 1
        assert records[0]["tool_name"] == "delegate_task"
        assert [(item["path"], item["operation"]) for item in records[0]["changes"]] == [
            ("tracked.txt", "modified")
        ]
    finally:
        manager.cleanup()
