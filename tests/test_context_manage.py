"""Tests for ManageContextTool."""

import asyncio
import os
import tempfile

import pytest

from coderAI.context import ContextManager
from coderAI.tools.context_manage import ManageContextTool


@pytest.fixture
def ctx_tool():
    """Create a ManageContextTool with a fresh ContextManager."""
    cm = ContextManager()
    return ManageContextTool(cm)


@pytest.fixture
def temp_file():
    """Create a small temp file for pinning."""
    fd, path = tempfile.mkstemp(suffix=".txt")
    os.write(fd, b"hello world")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.unlink(path)


class TestManageContextTool:
    """Tests for all ManageContextTool actions."""

    def test_list_empty(self, ctx_tool):
        result = asyncio.run(ctx_tool.execute(action="list"))
        assert result["success"] is True
        assert result["pinned_files"] == []

    def test_add_and_list(self, ctx_tool, temp_file):
        result = asyncio.run(ctx_tool.execute(action="add", path=temp_file))
        assert result["success"] is True

        result = asyncio.run(ctx_tool.execute(action="list"))
        assert len(result["pinned_files"]) == 1

    def test_add_nonexistent(self, ctx_tool):
        result = asyncio.run(ctx_tool.execute(action="add", path="/no/such/file.txt"))
        assert result["success"] is False

    def test_add_missing_path(self, ctx_tool):
        result = asyncio.run(ctx_tool.execute(action="add"))
        assert result["success"] is False

    def test_remove(self, ctx_tool, temp_file):
        asyncio.run(ctx_tool.execute(action="add", path=temp_file))
        result = asyncio.run(ctx_tool.execute(action="remove", path=temp_file))
        assert result["success"] is True

        result = asyncio.run(ctx_tool.execute(action="list"))
        assert result["pinned_files"] == []

    def test_remove_nonexistent(self, ctx_tool):
        result = asyncio.run(ctx_tool.execute(action="remove", path="/nope"))
        assert result["success"] is False

    def test_clear(self, ctx_tool, temp_file):
        asyncio.run(ctx_tool.execute(action="add", path=temp_file))
        result = asyncio.run(ctx_tool.execute(action="clear"))
        assert result["success"] is True

        result = asyncio.run(ctx_tool.execute(action="list"))
        assert result["pinned_files"] == []

    def test_unknown_action(self, ctx_tool):
        result = asyncio.run(ctx_tool.execute(action="nope"))
        assert result["success"] is False

    def test_add_oversized_file(self, ctx_tool):
        """Files > 100KB should be rejected."""
        fd, path = tempfile.mkstemp(suffix=".bin")
        try:
            os.write(fd, b"x" * (101 * 1024))
            os.close(fd)
            result = asyncio.run(ctx_tool.execute(action="add", path=path))
            assert result["success"] is False
        finally:
            os.unlink(path)
