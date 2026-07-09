"""Tests that path-accepting tools reject '..' traversal attempts."""

import asyncio
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _scope_strict(monkeypatch):
    """Clear the session-wide opt-out so scope enforcement applies."""
    monkeypatch.delenv("CODERAI_ALLOW_OUTSIDE_PROJECT", raising=False)
    from coderAI.system.config import config_manager

    config_manager._config = None


def _set_project_root(monkeypatch, root: Path):
    monkeypatch.setattr(
        "coderAI.system.config.config_manager.load_project_config",
        lambda _r: type("ProjectConfig", (), {"project_root": str(root)})(),
    )
    from coderAI.system.config import config_manager

    config_manager._config = None


TRAVERSAL_PATHS = [
    "../../../../etc/passwd",
    "subdir/../../../etc/passwd",
    "../..",
    "../../../..",
    "foo/../../bar/../../../etc/hostname",
]


# ---------------------------------------------------------------------------
# Filesystem tools with full scope enforcement
# ---------------------------------------------------------------------------


class TestReadFileTraversal:
    def test_rejects_dotdot_traversal(self, tmp_path, monkeypatch):
        _set_project_root(monkeypatch, tmp_path)
        from coderAI.tools.filesystem import ReadFileTool

        for rel in TRAVERSAL_PATHS:
            bad = str(tmp_path / rel)
            result = asyncio.run(ReadFileTool().execute(path=bad))
            assert result["success"] is False, f"should reject {rel}: {result}"
            assert result.get("error_code") == "scope", f"unexpected code for {rel}: {result}"


class TestWriteFileTraversal:
    def test_rejects_dotdot_traversal(self, tmp_path, monkeypatch):
        _set_project_root(monkeypatch, tmp_path)
        from coderAI.tools.filesystem import WriteFileTool

        for rel in TRAVERSAL_PATHS:
            bad = str(tmp_path / rel)
            result = asyncio.run(WriteFileTool().execute(path=bad, content="bad"))
            assert result["success"] is False, f"should reject {rel}"
            code = result.get("error_code")
            # scope or permission_denied depending on resolved path
            assert code in ("scope", "permission_denied"), f"unexpected code: {code}"


class TestDeleteFileTraversal:
    def test_rejects_dotdot_traversal(self, tmp_path, monkeypatch):
        _set_project_root(monkeypatch, tmp_path)
        from coderAI.tools.filesystem import DeleteFileTool

        for rel in TRAVERSAL_PATHS:
            bad = str(tmp_path / rel)
            result = asyncio.run(DeleteFileTool().execute(path=bad))
            assert result["success"] is False, f"should reject {rel}: {result}"


class TestListDirectoryTraversal:
    def test_rejects_dotdot_traversal(self, tmp_path, monkeypatch):
        _set_project_root(monkeypatch, tmp_path)
        from coderAI.tools.filesystem import ListDirectoryTool

        for rel in TRAVERSAL_PATHS:
            bad = str(tmp_path / rel)
            result = asyncio.run(ListDirectoryTool().execute(path=bad))
            assert result["success"] is False, f"should reject {rel}: {result}"
            assert result.get("error_code") == "scope"


class TestGlobSearchTraversal:
    def test_rejects_dotdot_traversal(self, tmp_path, monkeypatch):
        _set_project_root(monkeypatch, tmp_path)
        from coderAI.tools.filesystem import GlobSearchTool

        for rel in TRAVERSAL_PATHS:
            bad = str(tmp_path / rel)
            result = asyncio.run(GlobSearchTool().execute(pattern="*.py", base_path=bad))
            assert result["success"] is False, f"should reject {rel}: {result}"
            assert result.get("error_code") == "scope"


# ---------------------------------------------------------------------------
# RunCommandTool working_dir scope enforcement
# ---------------------------------------------------------------------------


class TestRunCommandTraversal:
    def test_rejects_outside_working_dir(self, tmp_path, monkeypatch):
        _set_project_root(monkeypatch, tmp_path)
        from coderAI.tools.terminal import RunCommandTool

        for rel in TRAVERSAL_PATHS:
            bad = str(tmp_path / rel)
            result = asyncio.run(RunCommandTool().execute(command="pwd", working_dir=bad))
            assert result["success"] is False, f"should reject {rel}: {result}"
            assert result.get("error_code") == "scope"


# ---------------------------------------------------------------------------
# Git tools scope enforcement
# ---------------------------------------------------------------------------


class TestGitScopeTraversal:
    def test_rejects_repo_path_outside_git_root(self, tmp_path, monkeypatch):
        """Git tools reject repo_path that resolves outside the git root."""
        from coderAI.tools.git import GitStatusTool

        _set_project_root(monkeypatch, tmp_path)
        (tmp_path / ".git").mkdir(exist_ok=True)
        (tmp_path / "subdir").mkdir(exist_ok=True)

        outside = str(tmp_path / "../..")
        result = asyncio.run(GitStatusTool().execute(repo_path=outside))
        assert result["success"] is False
        assert "scope" in result.get("error_code", "") or not result["success"]


# ---------------------------------------------------------------------------
# RefactorTool scope enforcement
# ---------------------------------------------------------------------------


class TestRefactorTraversal:
    def test_rejects_path_outside_project(self, tmp_path, monkeypatch):
        _set_project_root(monkeypatch, tmp_path)
        from coderAI.tools.refactor import RefactorTool

        bad = str(tmp_path / "../../etc/hosts")
        result = asyncio.run(
            RefactorTool().execute(
                path=bad,
                action="rename_symbol",
                symbol="old_name",
                new_name="new_name",
            )
        )
        assert result["success"] is False


# ---------------------------------------------------------------------------
# Search + project + metadata tools scope enforcement (finding 1)
# ---------------------------------------------------------------------------


class TestSearchToolsScope:
    """text_search / grep / symbol_search must not read outside the project."""

    def test_text_search_rejects_outside_project(self, tmp_path, monkeypatch):
        _set_project_root(monkeypatch, tmp_path)
        from coderAI.tools.search import TextSearchTool

        bad = str(tmp_path / "../..")
        result = asyncio.run(TextSearchTool().execute(query="secret", base_path=bad))
        assert result["success"] is False
        assert result.get("error_code") == "scope", result

    def test_grep_rejects_outside_project(self, tmp_path, monkeypatch):
        _set_project_root(monkeypatch, tmp_path)
        from coderAI.tools.search import GrepTool

        bad = str(tmp_path / "../..")
        result = asyncio.run(GrepTool().execute(pattern="secret", path=bad))
        assert result["success"] is False
        assert result.get("error_code") == "scope", result

    def test_symbol_search_rejects_outside_project(self, tmp_path, monkeypatch):
        _set_project_root(monkeypatch, tmp_path)
        from coderAI.tools.search import SymbolSearchTool

        bad = str(tmp_path / "../..")
        result = asyncio.run(SymbolSearchTool().execute(symbol="Agent", path=bad))
        assert result["success"] is False
        assert result.get("error_code") == "scope", result


class TestProjectContextScope:
    def test_rejects_outside_project(self, tmp_path, monkeypatch):
        _set_project_root(monkeypatch, tmp_path)
        from coderAI.tools.project import ProjectContextTool

        bad = str(tmp_path / "../..")
        result = asyncio.run(ProjectContextTool().execute(path=bad))
        assert result["success"] is False
        assert result.get("error_code") == "scope", result


class TestMetadataScope:
    """file_stat / file_readlink read parity with read_file scope."""

    def test_file_stat_rejects_outside_project(self, tmp_path, monkeypatch):
        _set_project_root(monkeypatch, tmp_path)
        from coderAI.tools.filesystem.metadata import FileStatTool

        bad = str(tmp_path / "../../etc/passwd")
        result = asyncio.run(FileStatTool().execute(path=bad))
        assert result["success"] is False
        assert result.get("error_code") == "scope", result

    def test_file_readlink_rejects_outside_project(self, tmp_path, monkeypatch):
        _set_project_root(monkeypatch, tmp_path)
        from coderAI.tools.filesystem.metadata import FileReadlinkTool

        bad = str(tmp_path / "../../etc/passwd")
        result = asyncio.run(FileReadlinkTool().execute(path=bad))
        assert result["success"] is False
        assert result.get("error_code") == "scope", result
