"""Tests for SharedNotepad and NotepadTool."""

import asyncio
import pytest

from coderAI.notepad import SharedNotepad
from coderAI.tools.notepad import NotepadTool


class TestSharedNotepad:
    def test_write_and_read(self):
        pad = SharedNotepad()
        pad.write("key1", "value1")
        note = pad.read("key1")
        assert note is not None
        assert note["value"] == "value1"

    def test_read_missing(self):
        pad = SharedNotepad()
        assert pad.read("missing") is None

    def test_list_keys(self):
        pad = SharedNotepad()
        pad.write("a", "1")
        pad.write("b", "2")
        keys = pad.list_keys()
        assert "a" in keys
        assert "b" in keys

    def test_delete(self):
        pad = SharedNotepad()
        pad.write("x", "y")
        assert pad.delete("x")
        assert not pad.delete("x")  # Already deleted

    def test_clear(self):
        pad = SharedNotepad()
        pad.write("a", "1")
        pad.write("b", "2")
        count = pad.clear()
        assert count == 2
        assert len(pad.list_keys()) == 0

    def test_read_all(self):
        pad = SharedNotepad()
        pad.write("a", "1")
        pad.write("b", "2")
        all_notes = pad.read_all()
        assert len(all_notes) == 2


class TestNotepadTool:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.tool = NotepadTool()

    def test_write_and_read(self):
        asyncio.run(
            self.tool.execute(action="write", key="test_key", value="test_value")
        )
        result = asyncio.run(
            self.tool.execute(action="read", key="test_key")
        )
        assert result["success"]
        assert result["value"] == "test_value"

    def test_list(self):
        result = asyncio.run(
            self.tool.execute(action="list")
        )
        assert result["success"]
        assert isinstance(result["keys"], list)

    def test_write_missing_key(self):
        result = asyncio.run(
            self.tool.execute(action="write", value="no_key")
        )
        assert not result["success"]

    def test_unknown_action(self):
        result = asyncio.run(
            self.tool.execute(action="invalid")
        )
        assert not result["success"]
