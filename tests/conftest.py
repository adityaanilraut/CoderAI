"""Shared test fixtures for CoderAI test suite."""

from __future__ import annotations

import os
import pathlib
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def temp_workspace(tmp_path: pathlib.Path) -> pathlib.Path:
    """Provide an isolated temporary workspace directory."""
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


@pytest.fixture
def mock_tool_context(temp_workspace: pathlib.Path) -> MagicMock:
    """Provide a mock tool execution context with standard attributes."""
    ctx = MagicMock()
    ctx.session_id = "test-session-123"
    ctx.project_root = str(temp_workspace)
    ctx.sandbox_mode = "workspace-write"
    ctx.create_openai_client = None
    ctx.on_before_file_mutation = None
    ctx.on_after_file_mutation = None
    ctx.on_process_start = None
    ctx.on_process_exit = None
    return ctx


@pytest.fixture
def isolated_home(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Redirect HOME to tmp and strip host model/credential env vars."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    for var in list(os.environ):
        if var.startswith("CODERAI_") or "MODEL" in var or "API_KEY" in var or "BASE_URL" in var:
            monkeypatch.delenv(var, raising=False)
    return home


@pytest.fixture
def cli_isolated_home(
    isolated_home: pathlib.Path, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> pathlib.Path:
    """Isolated HOME plus clean cwd for CLI tests."""
    monkeypatch.chdir(tmp_path)
    return isolated_home
