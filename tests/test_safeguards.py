"""Tests for safety guards and validation in CoderAI."""

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

from coderAI.safeguards import (
    filter_stageable_files,
    is_interactive_command,
    project_sanity_check,
    resolve_git_root,
    get_current_branch,
)


# ============================================================================
# Interactive Command Detection
# ============================================================================


class TestIsInteractiveCommand:
    """Tests for is_interactive_command()."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "python",
            "python3",
            "node",
            "irb",
            "vim file.txt",
            "nano ~/.bashrc",
            "top",
            "htop",
            "ssh user@host",
            "psql",
            "mysql",
            "mongo",
            "less file.txt",
            "coderai",
            "bash",
            "zsh",
        ],
    )
    def test_interactive_commands_detected(self, cmd):
        assert is_interactive_command(cmd), f"Expected {cmd!r} to be interactive"

    @pytest.mark.parametrize(
        "cmd",
        [
            "python -c 'print(1)'",
            "python script.py",
            "python3 -m pytest",
            "python3 --version",
            "node -e 'console.log(1)'",
            "node server.js",
            "echo hello",
            "git status",
            "ls -la",
            "cat file.txt",
            "bash script.sh",
            "bash -c 'echo 1'",
            "bash -lc 'echo 1'",
            "zsh -c 'echo 1'",
            "python -m http.server",
            "psql -f script.sql",
            "python3 --help",
            "python -",
            "node -",
        ],
    )
    def test_non_interactive_commands_allowed(self, cmd):
        assert not is_interactive_command(cmd), f"Expected {cmd!r} to NOT be interactive"

    def test_empty_command(self):
        assert not is_interactive_command("")
        assert not is_interactive_command("   ")

    def test_docker_interactive_flag(self):
        assert is_interactive_command("docker run -it ubuntu bash")
        assert is_interactive_command("docker exec -it container sh")

    def test_env_prefix_handled(self):
        assert is_interactive_command("env python")
        assert not is_interactive_command("env python script.py")

    def test_shell_combined_dash_c_non_interactive(self):
        assert not is_interactive_command("bash -lc 'echo ok'")

    def test_shell_login_without_c_still_interactive(self):
        assert is_interactive_command("bash -l")

    def test_heredoc_not_interactive(self):
        assert not is_interactive_command("python - <<'PY'\nprint(1)\nPY")


# ============================================================================
# Project Sanity Check
# ============================================================================


class TestProjectSanityCheck:
    """Tests for project_sanity_check()."""

    def test_empty_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = project_sanity_check(tmpdir)
            assert result["is_valid_project"] is False
            assert len(result["reasons"]) > 0

    def test_directory_with_only_ds_store(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, ".DS_Store").touch()
            result = project_sanity_check(tmpdir)
            assert result["is_valid_project"] is False

    def test_directory_with_package_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "package.json").write_text("{}")
            result = project_sanity_check(tmpdir)
            assert result["is_valid_project"] is True
            assert "package.json" in result["detected_files"]

    def test_directory_with_pyproject_toml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "pyproject.toml").write_text("[project]")
            result = project_sanity_check(tmpdir)
            assert result["is_valid_project"] is True
            assert "pyproject.toml" in result["detected_files"]

    def test_directory_with_source_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "main.py").write_text("print('hello')")
            result = project_sanity_check(tmpdir)
            assert result["is_valid_project"] is True

    def test_directory_with_source_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = Path(tmpdir, "src")
            src_dir.mkdir()
            Path(src_dir, "main.py").write_text("print('hello')")
            result = project_sanity_check(tmpdir)
            assert result["is_valid_project"] is True
            assert "src" in result["detected_source_dirs"]

    def test_nonexistent_directory(self):
        result = project_sanity_check("/nonexistent/dir/xyz_123")
        assert result["is_valid_project"] is False

    def test_has_git_flag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, ".git").mkdir()
            Path(tmpdir, "main.py").write_text("x = 1")
            result = project_sanity_check(tmpdir)
            assert result["has_git"] is True

    def test_no_git_flag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "main.py").write_text("x = 1")
            result = project_sanity_check(tmpdir)
            assert result["has_git"] is False


# ============================================================================
# Git Root / Scope Safety
# ============================================================================


class TestResolveGitRoot:
    """Tests for resolve_git_root() and get_current_branch()."""

    @pytest.fixture
    def git_repo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            asyncio.run(self._init_repo(tmpdir))
            yield tmpdir

    async def _init_repo(self, path):
        for cmd_args in [
            ["git", "init"],
            ["git", "config", "user.email", "test@test.com"],
            ["git", "config", "user.name", "Test"],
        ]:
            proc = await asyncio.create_subprocess_exec(
                *cmd_args, cwd=path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

    def test_valid_git_repo_matches(self, git_repo):
        result = asyncio.run(resolve_git_root(git_repo))
        assert result["git_root"] is not None
        assert result["matches_expected"] is True
        assert result["warning"] is None

    def test_non_git_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = asyncio.run(resolve_git_root(tmpdir))
            assert result["git_root"] is None
            assert result["matches_expected"] is False
            assert result["warning"] is not None

    def test_nested_dir_scope_mismatch(self, git_repo):
        """A subdirectory inside a git repo should report scope mismatch."""
        sub_dir = os.path.join(git_repo, "sub", "nested")
        os.makedirs(sub_dir)
        result = asyncio.run(resolve_git_root(sub_dir))
        assert result["git_root"] is not None
        assert result["matches_expected"] is False
        assert "mismatch" in result["warning"].lower()

    def test_get_current_branch(self, git_repo):
        # Create an initial commit so HEAD exists
        filepath = os.path.join(git_repo, "README.md")
        with open(filepath, "w") as f:
            f.write("# Test")
        asyncio.run(self._stage_and_commit(git_repo, filepath))

        branch = asyncio.run(get_current_branch(git_repo))
        assert branch is not None
        assert isinstance(branch, str)
        assert len(branch) > 0

    async def _stage_and_commit(self, repo_path, filepath):
        for cmd_args in [
            ["git", "add", filepath],
            ["git", "commit", "-m", "init"],
        ]:
            proc = await asyncio.create_subprocess_exec(
                *cmd_args, cwd=repo_path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()


# ============================================================================
# Staging Filter
# ============================================================================


class TestFilterStageableFiles:
    """Tests for filter_stageable_files()."""

    def test_ds_store_filtered(self):
        allowed, rejected = filter_stageable_files([".DS_Store", "src/main.py"])
        assert ".DS_Store" in rejected
        assert "src/main.py" in allowed

    def test_pycache_filtered(self):
        allowed, rejected = filter_stageable_files(["__pycache__/foo.pyc", "app.py"])
        assert "__pycache__/foo.pyc" in rejected
        assert "app.py" in allowed

    def test_pyc_filtered(self):
        allowed, rejected = filter_stageable_files(["module.pyc", "module.py"])
        assert "module.pyc" in rejected
        assert "module.py" in allowed

    def test_coderAI_internal_filtered(self):
        allowed, rejected = filter_stageable_files([".coderAI/tasks.json", "src/app.py"])
        assert ".coderAI/tasks.json" in rejected
        assert "src/app.py" in allowed

    def test_clean_files_pass_through(self):
        files = ["src/main.py", "README.md", "tests/test_app.py"]
        allowed, rejected = filter_stageable_files(files)
        assert allowed == files
        assert rejected == []

    def test_all_junk_returns_empty(self):
        files = [".DS_Store", "__pycache__/x.pyc", ".env"]
        allowed, rejected = filter_stageable_files(files)
        assert len(allowed) == 0
        assert len(rejected) == 3

    def test_env_filtered(self):
        allowed, rejected = filter_stageable_files([".env", ".env.local", "config.py"])
        assert ".env" in rejected
        assert ".env.local" in rejected
        assert "config.py" in allowed

    def test_node_modules_filtered(self):
        allowed, rejected = filter_stageable_files(
            ["node_modules/lodash/index.js", "src/app.js"]
        )
        assert "node_modules/lodash/index.js" in rejected
        assert "src/app.js" in allowed


# ============================================================================
# Git Add Safety (Integration)
# ============================================================================


class TestGitAddSafety:
    """Tests for GitAddTool safety features."""

    def test_git_add_dot_rejected(self):
        from coderAI.tools.git import GitAddTool

        tool = GitAddTool()
        result = asyncio.run(tool.execute(files=["."], repo_path="."))
        assert result["success"] is False
        assert result.get("error_code") == "unsafe_staging"

    def test_git_add_star_rejected(self):
        from coderAI.tools.git import GitAddTool

        tool = GitAddTool()
        result = asyncio.run(tool.execute(files=["*"], repo_path="."))
        assert result["success"] is False
        assert result.get("error_code") == "unsafe_staging"


# ============================================================================
# Interactive Command Blocking (Integration)
# ============================================================================


class TestInteractiveCommandBlocking:
    """Tests that RunCommandTool blocks interactive commands."""

    def test_run_interactive_command_blocked(self):
        from coderAI.tools.terminal import RunCommandTool

        tool = RunCommandTool()
        result = asyncio.run(tool.execute(command="python"))
        assert result["success"] is False
        assert result.get("interactive") is True
        assert result.get("error_code") == "interactive"

    def test_run_noninteractive_command_allowed(self):
        from coderAI.tools.terminal import RunCommandTool

        tool = RunCommandTool()
        result = asyncio.run(tool.execute(command="echo 'hello'"))
        assert result["success"] is True
