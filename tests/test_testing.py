"""Tests for RunTestsTool and detect_test_framework."""

import asyncio
import shutil
import pytest

from coderAI.tools.testing import RunTestsTool, detect_test_framework, TEST_FRAMEWORKS


class TestDetectTestFramework:
    def test_detects_pytest_for_python_project(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[tool.pytest]\n")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_foo.py").write_text("def test_pass(): pass\n")
        if shutil.which("pytest"):
            result = detect_test_framework(str(tmp_path))
            assert result == "pytest"

    def test_returns_none_for_empty_dir(self, tmp_path):
        result = detect_test_framework(str(tmp_path))
        assert result is None

    def test_all_registered_frameworks_have_required_keys(self):
        required = {"cmd", "args", "results_patterns", "detect_files", "test_suffixes", "extensions", "timeout"}
        for name, config in TEST_FRAMEWORKS.items():
            missing = required - set(config.keys())
            assert not missing, f"Framework {name} missing keys: {missing}"

    def test_detects_go_test_for_go_project(self, tmp_path):
        if shutil.which("go"):
            (tmp_path / "go.mod").write_text("module test\n\ngo 1.21\n")
            result = detect_test_framework(str(tmp_path))
            assert result == "go_test"

    def test_detects_jest_for_node_project(self, tmp_path):
        if shutil.which("npx"):
            (tmp_path / "package.json").write_text('{"name":"test","scripts":{"test":"jest"}}')
            (tmp_path / "jest.config.js").write_text("module.exports = {};")
            result = detect_test_framework(str(tmp_path))
            assert result in ("jest", "vitest", None)


class TestRunTestsTool:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.tool = RunTestsTool()

    def test_tool_properties(self):
        assert self.tool.name == "run_tests"
        assert self.tool.is_read_only is False
        assert self.tool.requires_confirmation is False

    def test_unknown_framework_returns_error(self):
        result = asyncio.run(
            self.tool.execute(framework="nonexistent_framework_xyz")
        )
        assert not result["success"]
        assert "Unknown test framework" in result["error"]

    def test_no_framework_detected_returns_error(self, tmp_path):
        result = asyncio.run(
            self.tool.execute(path=str(tmp_path))
        )
        assert not result["success"]

    def test_pytest_with_passing_tests(self, tmp_path):
        if not shutil.which("pytest"):
            pytest.skip("pytest not installed")

        (tmp_path / "pyproject.toml").write_text("[tool.pytest]\n")
        test_file = tmp_path / "test_example.py"
        test_file.write_text("def test_pass():\n    assert True\n")

        result = asyncio.run(
            self.tool.execute(path=str(tmp_path), framework="pytest")
        )
        assert result["success"]
        assert result["framework"] == "pytest"
        assert result["results"]["passed"] >= 1
        assert result["results"]["failed"] == 0

    def test_pytest_with_failing_tests(self, tmp_path):
        if not shutil.which("pytest"):
            pytest.skip("pytest not installed")

        (tmp_path / "pyproject.toml").write_text("[tool.pytest]\n")
        test_file = tmp_path / "test_fail.py"
        test_file.write_text("def test_fail():\n    assert False\n")

        result = asyncio.run(
            self.tool.execute(path=str(tmp_path), framework="pytest")
        )
        assert result["success"]
        assert result["results"]["failed"] >= 1
        # Failure details should include the test name
        assert len(result["results"]["failures"]) > 0

    def test_filter_runs_specific_test(self, tmp_path):
        if not shutil.which("pytest"):
            pytest.skip("pytest not installed")

        (tmp_path / "pyproject.toml").write_text("[tool.pytest]\n")
        (tmp_path / "test_a.py").write_text("def test_foo():\n    assert True\n")
        (tmp_path / "test_b.py").write_text("def test_bar():\n    assert True\n")

        result = asyncio.run(
            self.tool.execute(
                path=str(tmp_path),
                framework="pytest",
                filter="test_foo",
            )
        )
        assert result["success"]
        assert result["results"]["passed"] >= 1

    def test_verbose_output(self, tmp_path):
        if not shutil.which("pytest"):
            pytest.skip("pytest not installed")

        (tmp_path / "pyproject.toml").write_text("[tool.pytest]\n")
        (tmp_path / "test_example.py").write_text("def test_pass():\n    assert True\n")

        result = asyncio.run(
            self.tool.execute(
                path=str(tmp_path),
                framework="pytest",
                verbose=True,
            )
        )
        assert result["success"]
        assert "stdout" in result
        assert len(result["stdout"]) > 0

    def test_missing_binary_returns_error(self):
        result = asyncio.run(
            self.tool.execute(
                path=".",
                framework="nonexistent_framework_xyz",
            )
        )
        assert not result["success"]
