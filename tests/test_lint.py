"""Tests for LintTool and detect_linter."""

import asyncio
import shutil
import pytest

from coderAI.tools.lint import LintTool, detect_linter


@pytest.fixture
def python_project(tmp_path):
    """A minimal Python project directory with a ruff-detectable indicator."""
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n")
    (tmp_path / "main.py").write_text("x=1\n")
    return tmp_path


class TestDetectLinter:
    def test_detects_ruff_for_python_project(self, python_project):
        if not shutil.which("ruff"):
            pytest.skip("ruff not installed")
        result = detect_linter(str(python_project))
        assert result == "ruff"

    def test_returns_none_for_empty_dir(self, tmp_path):
        result = detect_linter(str(tmp_path))
        assert result is None

    def test_detects_eslint_for_node_project(self, tmp_path):
        if not shutil.which("npx"):
            pytest.skip("npx not installed")
        (tmp_path / "package.json").write_text('{"name":"test"}')
        (tmp_path / ".eslintrc.json").write_text("{}")
        # eslint may not be available; just check it doesn't raise
        result = detect_linter(str(tmp_path))
        # result is either "eslint" or None depending on environment
        assert result in ("eslint", None)


class TestLintTool:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.tool = LintTool()

    def test_unknown_linter_returns_error(self):
        result = asyncio.run(
            self.tool.execute(path=".", linter="nonexistent_linter_xyz")
        )
        assert not result["success"]
        assert "Unknown linter" in result["error"]

    def test_no_linter_detected_returns_error(self, tmp_path):
        result = asyncio.run(
            self.tool.execute(path=str(tmp_path))
        )
        assert not result["success"]

    def test_ruff_check_on_python_file(self, tmp_path):
        if not shutil.which("ruff"):
            pytest.skip("ruff not installed")
        py_file = tmp_path / "bad.py"
        py_file.write_text("import os\nimport sys\nx=1\n")
        result = asyncio.run(
            self.tool.execute(path=str(py_file), linter="ruff")
        )
        assert result["success"]
        assert result["linter"] == "ruff"
        assert result["mode"] == "check"

    def test_ruff_fix_mode(self, tmp_path):
        if not shutil.which("ruff"):
            pytest.skip("ruff not installed")
        py_file = tmp_path / "fixable.py"
        py_file.write_text("import os\nx=1\n")
        result = asyncio.run(
            self.tool.execute(path=str(py_file), linter="ruff", fix=True)
        )
        assert result["success"]
        assert result["mode"] == "fix"

    def test_missing_binary_returns_error(self):
        result = asyncio.run(
            self.tool.execute(path=".", linter="golangci-lint")
        )
        # Either detected or error about binary not found
        if not shutil.which("golangci-lint"):
            assert not result["success"]
            assert "not found" in result["error"]
