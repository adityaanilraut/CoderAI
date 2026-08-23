"""Tests for Isolated Python Code Runtime."""

from __future__ import annotations

import pytest
from coderai.core.code_mode.runtime import PythonCodeRuntime, validate_lossless_json_value


def test_lossless_json_validation():
    assert validate_lossless_json_value(123) is True
    assert validate_lossless_json_value("test") is True
    assert validate_lossless_json_value([1, 2, {"a": 3}]) is True
    assert validate_lossless_json_value(float("nan")) is False
    assert validate_lossless_json_value(float("inf")) is False
    assert validate_lossless_json_value(object()) is False


@pytest.mark.asyncio
async def test_python_code_runtime_execution(tmp_path):
    runtime = PythonCodeRuntime(project_root=str(tmp_path))

    # Basic print and return value
    code = """
x = 10
y = 20
print("Calculating sum...")
x + y
"""
    res = await runtime.execute(code)
    assert res.success is True
    assert res.result == 30
    assert "Calculating sum..." in res.stdout
    assert "x" in res.variables
    assert "y" in res.variables

    # Stateful retention across turns
    code_turn2 = """
z = x * 2
z
"""
    res2 = await runtime.execute(code_turn2)
    assert res2.success is True
    assert res2.result == 20
    assert "z" in res2.variables


@pytest.mark.asyncio
async def test_python_code_runtime_timeout(tmp_path):
    runtime = PythonCodeRuntime(project_root=str(tmp_path))
    code = """
import time
time.sleep(2.0)
"""
    res = await runtime.execute(code, timeout_seconds=0.1)
    assert res.success is False
    assert "TimeoutError" in (res.error or "")
