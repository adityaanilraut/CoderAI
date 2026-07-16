"""Tests for extended Git tools: GitBranchTool, GitCheckoutTool, GitStashTool."""

import asyncio
import os
import subprocess
from unittest.mock import AsyncMock, patch

import pytest

from coderAI.tools.git import (
    GIT_NETWORK_TIMEOUT_SECONDS,
    GitBranchTool,
    GitStatusTool,
)
from coderAI.tools.git_extended import (
    GitCheckoutTool,
    GitPushTool,
    GitStashTool,
)


@pytest.fixture
def git_repo(tmp_path):
    """Create a temporary git repo for testing."""
    repo = tmp_path / "test_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True
    )
    # Create initial commit
    (repo / "file.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    return str(repo)


class TestGitBranchTool:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.tool = GitBranchTool()

    def test_list_branches(self, git_repo):
        result = asyncio.run(self.tool.execute(action="list", repo_path=git_repo))
        assert result["success"]
        assert len(result["branches"]) >= 1

    def test_create_branch(self, git_repo):
        result = asyncio.run(
            self.tool.execute(action="create", branch_name="feature-x", repo_path=git_repo)
        )
        assert result["success"]

    def test_create_branch_missing_name(self, git_repo):
        result = asyncio.run(self.tool.execute(action="create", repo_path=git_repo))
        assert not result["success"]

    def test_delete_branch(self, git_repo):
        # Create then delete
        asyncio.run(self.tool.execute(action="create", branch_name="to-delete", repo_path=git_repo))
        result = asyncio.run(
            self.tool.execute(action="delete", branch_name="to-delete", repo_path=git_repo)
        )
        assert result["success"]


class TestGitCheckoutTool:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.tool = GitCheckoutTool()

    def test_checkout_new_branch(self, git_repo):
        result = asyncio.run(
            self.tool.execute(branch="new-branch", create=True, repo_path=git_repo)
        )
        assert result["success"]

    def test_checkout_existing_branch(self, git_repo):
        # Create branch first
        subprocess.run(["git", "branch", "existing"], cwd=git_repo, check=True, capture_output=True)
        result = asyncio.run(self.tool.execute(branch="existing", repo_path=git_repo))
        assert result["success"]


class TestGitStashTool:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.tool = GitStashTool()

    def test_stash_list_empty(self, git_repo):
        result = asyncio.run(self.tool.execute(action="list", repo_path=git_repo))
        assert result["success"]

    def test_stash_push_and_pop(self, git_repo):
        # Create a change to stash
        with open(os.path.join(git_repo, "file.txt"), "w") as f:
            f.write("modified")
        result = asyncio.run(
            self.tool.execute(action="push", message="test stash", repo_path=git_repo)
        )
        assert result["success"]
        # Pop it back
        result = asyncio.run(self.tool.execute(action="pop", repo_path=git_repo))
        assert result["success"]


class TestGitSubprocessTimeouts:
    """git subprocesses previously ran unbounded; every run_scrubbed call must
    now carry a timeout — the config default locally, the wider network
    constant for push/pull/fetch."""

    def _scrubbed_mock(self):
        return AsyncMock(return_value=(0, b"", b"", False))

    def test_git_status_passes_default_timeout(self, git_repo):
        mock = self._scrubbed_mock()
        with patch("coderAI.tools.git.run_scrubbed", mock):
            result = asyncio.run(GitStatusTool().execute(repo_path=git_repo))
        assert result["success"]
        timeout = mock.call_args.kwargs["timeout"]
        assert timeout is not None and timeout > 0

    def test_git_push_passes_network_timeout(self, git_repo):
        mock = self._scrubbed_mock()
        with patch("coderAI.tools.git.run_scrubbed", mock):
            result = asyncio.run(GitPushTool().execute(repo_path=git_repo))
        assert result["success"]
        assert mock.call_args.kwargs["timeout"] == GIT_NETWORK_TIMEOUT_SECONDS

    def test_network_tools_outer_cap_sits_above_inner_timeout(self):
        # The executor's outer wait_for must never fire before run_scrubbed's
        # own group-kill cleanup at GIT_NETWORK_TIMEOUT_SECONDS.
        assert GitPushTool.timeout > GIT_NETWORK_TIMEOUT_SECONDS

    def test_timed_out_git_command_reports_timeout(self, git_repo):
        mock = AsyncMock(return_value=(124, b"", b"", True))
        with patch("coderAI.tools.git.run_scrubbed", mock):
            result = asyncio.run(GitStatusTool().execute(repo_path=git_repo))
        assert result["success"] is False
        assert result["error_code"] == "timeout"
