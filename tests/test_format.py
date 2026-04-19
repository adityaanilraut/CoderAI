"""Tests for FormatTool and detect_formatter."""

import asyncio
import shutil
import pytest

from coderAI.tools.format import FormatTool, detect_formatter, FORMATTERS


@pytest.fixture
def python_project(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n")
    (tmp_path / "main.py").write_text('x=1\ny=2\n')
    return tmp_path


class TestDetectFormatter:
    def test_detects_ruff_for_python_project(self, python_project):
        if not shutil.which("ruff"):
            pytest.skip("ruff not installed")
        result = detect_formatter(str(python_project))
        assert result == "ruff"

    def test_returns_none_for_empty_dir(self, tmp_path):
        result = detect_formatter(str(tmp_path))
        assert result is None

    def test_respects_preference_order(self, tmp_path):
        """ruff should be preferred over black when both indicators exist."""
        if not shutil.which("ruff"):
            pytest.skip("ruff not installed")
        (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n")
        (tmp_path / "setup.py").write_text("")
        result = detect_formatter(str(tmp_path))
        assert result in ("ruff", "black", None)


class TestFormatTool:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.tool = FormatTool()

    def test_unknown_formatter_returns_error(self):
        result = asyncio.run(
            self.tool.execute(path=".", formatter="nonexistent_formatter_xyz")
        )
        assert not result["success"]
        assert "Unknown formatter" in result["error"]

    def test_no_formatter_detected_returns_error(self, tmp_path):
        result = asyncio.run(self.tool.execute(path=str(tmp_path)))
        assert not result["success"]

    def test_missing_binary_returns_error(self):
        result = asyncio.run(
            self.tool.execute(path=".", formatter="gofmt")
        )
        if not shutil.which("gofmt"):
            assert not result["success"]
            assert "not found" in result["error"]

    def test_ruff_format_check_mode(self, python_project):
        if not shutil.which("ruff"):
            pytest.skip("ruff not installed")
        result = asyncio.run(
            self.tool.execute(path=str(python_project), formatter="ruff", check=True)
        )
        assert result["success"]
        assert result["mode"] == "check"
        assert "needs_formatting" in result

    def test_ruff_format_write_mode(self, python_project):
        if not shutil.which("ruff"):
            pytest.skip("ruff not installed")
        result = asyncio.run(
            self.tool.execute(path=str(python_project), formatter="ruff", check=False)
        )
        assert result["mode"] == "format"

    def test_result_has_formatter_field(self, python_project):
        if not shutil.which("ruff"):
            pytest.skip("ruff not installed")
        result = asyncio.run(
            self.tool.execute(path=str(python_project), formatter="ruff", check=True)
        )
        assert result.get("formatter") == "ruff"

    def test_formatters_dict_completeness(self):
        for name, cfg in FORMATTERS.items():
            assert "cmd" in cfg
            assert "args" in cfg
            assert "check_args" in cfg
            assert "extensions" in cfg
            assert "detect_files" in cfg
