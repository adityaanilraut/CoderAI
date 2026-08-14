"""Unit tests for Phase 3 features:
- Non-git delegation worktree fallback
- Session branching / forking (/fork and HistoryManager.fork_session)
- TUI slash command routing for /fork
- Objective mutation tracking expansion
"""

from pathlib import Path

from coderAI.core.delegation_worktree import DelegationWorktree
from coderAI.system.history import HistoryManager
from coderAI.core.objective import _WORKSPACE_MUTATION_TOOLS, _CHECK_COMMAND


def test_non_git_delegation_worktree(tmp_path: Path):
    # Setup non-git directory with source files
    project_dir = tmp_path / "plain_project"
    project_dir.mkdir()
    (project_dir / "app.py").write_text("print('hello world')\n")
    (project_dir / "utils.py").write_text("def add(a, b): return a + b\n")

    storage_dir = tmp_path / "worktrees"

    worktree = DelegationWorktree(
        parent_root=project_dir,
        worktree_id="del_test_1",
        storage_root=storage_dir,
        allow_non_git=True,
    )
    assert worktree._is_git is False

    ws_path = worktree.create()
    assert ws_path.exists()
    assert (ws_path / "app.py").exists()
    assert (ws_path / "utils.py").exists()

    # Make a change in the isolated workspace
    (ws_path / "app.py").write_text("print('hello modified')\n")
    (ws_path / "new_file.txt").write_text("fresh file content\n")

    patch = worktree.prepare_patch()
    assert len(patch.changes) == 2
    ops = {c.path: c.operation for c in patch.changes}
    assert ops.get("app.py") == "modified"
    assert ops.get("new_file.txt") == "added"

    # Cleanup
    worktree.cleanup()
    assert not ws_path.exists()


def test_session_forking(tmp_path: Path):
    mgr = HistoryManager(history_dir=tmp_path / "history")
    s1 = mgr.create_session(model="test-model")
    s1.name = "Original Session"

    # Turn 1
    s1.add_checkpoint("Turn 1: hello")
    s1.add_message("user", "Turn 1: hello")
    s1.add_message("assistant", "Hi there!")

    # Turn 2
    s1.add_checkpoint("Turn 2: write code")
    s1.add_message("user", "Turn 2: write code")
    s1.add_message("assistant", "Here is code.")

    mgr.save_session(s1)

    # Full fork
    s_fork = mgr.fork_session(source_session_id=s1.session_id)
    assert s_fork.session_id != s1.session_id
    assert s_fork.parent_session_id == s1.session_id
    assert len(s_fork.messages) == 4
    assert s_fork.name == "Fork of Original Session"

    # Turn-bounded fork (fork at turn 2 -> keep up to start of turn 2)
    s_turn_fork = mgr.fork_session(source_session_id=s1.session_id, up_to_turn=2)
    assert s_turn_fork.session_id != s1.session_id
    assert len(s_turn_fork.messages) == 2
    assert s_turn_fork.messages[0].content == "Turn 1: hello"
    assert s_turn_fork.messages[1].content == "Hi there!"


def test_objective_mutation_and_verification_catalogs():
    assert "multi_edit" in _WORKSPACE_MUTATION_TOOLS
    assert "search_replace" in _WORKSPACE_MUTATION_TOOLS
    assert "write_file" in _WORKSPACE_MUTATION_TOOLS
    assert "edit_file" not in _WORKSPACE_MUTATION_TOOLS
    assert "replace_file_content" not in _WORKSPACE_MUTATION_TOOLS
    assert "multi_replace_file_content" not in _WORKSPACE_MUTATION_TOOLS
    assert "patch_file" not in _WORKSPACE_MUTATION_TOOLS

    # Check verify command regex
    assert _CHECK_COMMAND.search("pytest -v") is not None
    assert _CHECK_COMMAND.search("ruff check .") is not None
    assert _CHECK_COMMAND.search("biome lint") is not None
    assert _CHECK_COMMAND.search("golangci-lint run") is not None
    assert _CHECK_COMMAND.search("cargo clippy") is not None
    assert _CHECK_COMMAND.search("shellcheck script.sh") is not None
