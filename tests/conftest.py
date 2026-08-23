"""Shared test fixtures for CoderAI test suite."""

from __future__ import annotations

import pathlib
import tempfile
from typing import Any
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
