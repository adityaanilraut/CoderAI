"""Tests for extended Git tools: GitBranchTool, GitCheckoutTool, GitStashTool."""

import asyncio
import os
import subprocess
import tempfile
import pytest

from coderAI.tools.git import GitBranchTool, GitCheckoutTool, GitStashTool


@pytest.fixture
def git_repo(tmp_path):
    """Create a temporary git repo for testing."""
    repo = tmp_path / "test_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
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
        result = asyncio.run(
            self.tool.execute(action="list", repo_path=git_repo)
        )
        assert result["success"]
        assert len(result["branches"]) >= 1

    def test_create_branch(self, git_repo):
        result = asyncio.run(
            self.tool.execute(action="create", branch_name="feature-x", repo_path=git_repo)
        )
        assert result["success"]

    def test_create_branch_missing_name(self, git_repo):
        result = asyncio.run(
            self.tool.execute(action="create", repo_path=git_repo)
        )
        assert not result["success"]

    def test_delete_branch(self, git_repo):
        # Create then delete
        asyncio.run(
            self.tool.execute(action="create", branch_name="to-delete", repo_path=git_repo)
        )
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
        result = asyncio.run(
            self.tool.execute(branch="existing", repo_path=git_repo)
        )
        assert result["success"]


class TestGitStashTool:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.tool = GitStashTool()

    def test_stash_list_empty(self, git_repo):
        result = asyncio.run(
            self.tool.execute(action="list", repo_path=git_repo)
        )
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
        result = asyncio.run(
            self.tool.execute(action="pop", repo_path=git_repo)
        )
        assert result["success"]
