"""Tests for PythonREPLTool."""

import asyncio
import pytest

from coderAI.tools.repl import PythonREPLTool


class TestPythonREPLTool:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.tool = PythonREPLTool()

    def test_simple_print(self):
        result = asyncio.run(
            self.tool.execute(code='print("hello world")')
        )
        assert result["success"]
        assert "hello world" in result["stdout"]

    def test_math_expression(self):
        result = asyncio.run(
            self.tool.execute(code='print(2 + 2)')
        )
        assert result["success"]
        assert "4" in result["stdout"]

    def test_multiline_script(self):
        code = """
x = [1, 2, 3, 4, 5]
print(f"Sum: {sum(x)}")
print(f"Len: {len(x)}")
"""
        result = asyncio.run(
            self.tool.execute(code=code)
        )
        assert result["success"]
        assert "Sum: 15" in result["stdout"]
        assert "Len: 5" in result["stdout"]

    def test_syntax_error(self):
        result = asyncio.run(
            self.tool.execute(code='def foo(')
        )
        assert not result["success"]
        assert result["returncode"] != 0

    def test_timeout(self):
        result = asyncio.run(
            self.tool.execute(code='import time; time.sleep(10)', timeout=2)
        )
        assert not result["success"]
        assert "timeout" in result.get("error_code", "") or "timed out" in result.get("error", "")

    def test_import_stdlib(self):
        result = asyncio.run(
            self.tool.execute(code='import json; print(json.dumps({"a": 1}))')
        )
        assert result["success"]
        assert '"a": 1' in result["stdout"]
