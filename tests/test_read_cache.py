"""Tests for the per-session file-read dedup cache."""

import asyncio
import os
import tempfile
import time
from pathlib import Path

import pytest

from coderAI.read_cache import FileReadCache
from coderAI.tools.filesystem import ReadFileTool


@pytest.fixture
def tmp_file():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "sample.py"
        p.write_text("def hello():\n    return 'world'\n")
        yield p


def _read(tool: ReadFileTool, path: Path, **kwargs):
    return asyncio.run(tool.execute(path=str(path), **kwargs))


class TestFileReadCachePrimitive:
    def test_empty_cache_misses(self):
        cache = FileReadCache()
        assert cache.check("/tmp/x", 1.0, 10) is None

    def test_record_then_check_hits(self):
        cache = FileReadCache()
        cache.bump_turn()
        cache.record("/tmp/x", 1.0, 10)
        assert cache.check("/tmp/x", 1.0, 10) == 1

    def test_size_change_misses(self):
        cache = FileReadCache()
        cache.bump_turn()
        cache.record("/tmp/x", 1.0, 10)
        assert cache.check("/tmp/x", 1.0, 11) is None

    def test_mtime_change_misses(self):
        cache = FileReadCache()
        cache.bump_turn()
        cache.record("/tmp/x", 1.0, 10)
        assert cache.check("/tmp/x", 2.0, 10) is None

    def test_clear_drops_entries_and_resets_turn(self):
        cache = FileReadCache()
        cache.bump_turn()
        cache.bump_turn()
        cache.record("/tmp/x", 1.0, 10)
        cache.clear()
        assert cache.check("/tmp/x", 1.0, 10) is None
        assert cache.turn == 0

    def test_record_without_bump_uses_turn_one(self):
        # Tools that exercise the cache directly (without a real agent loop)
        # shouldn't crash with a "turn 0" placeholder — record falls back to 1.
        cache = FileReadCache()
        cache.record("/tmp/x", 1.0, 10)
        assert cache.check("/tmp/x", 1.0, 10) == 1


class TestReadFileToolWithCache:
    def test_repeat_read_returns_placeholder(self, tmp_file):
        cache = FileReadCache()
        cache.bump_turn()  # turn 1
        tool = ReadFileTool()
        tool.read_cache = cache

        first = _read(tool, tmp_file)
        assert first["success"] is True
        assert first.get("cached") is not True
        assert "hello" in first["content"]

        cache.bump_turn()  # turn 2
        second = _read(tool, tmp_file)
        assert second["success"] is True
        assert second.get("cached") is True
        assert "unchanged" in second["content"]
        assert "turn 1" in second["content"]
        # Placeholder must be non-empty so tool_use_id pairing in the
        # downstream message stays intact.
        assert second["content"].strip() != ""

    def test_edit_between_reads_invalidates_cache(self, tmp_file):
        cache = FileReadCache()
        cache.bump_turn()
        tool = ReadFileTool()
        tool.read_cache = cache

        first = _read(tool, tmp_file)
        assert first.get("cached") is not True

        # Make sure mtime advances even on fast filesystems.
        time.sleep(0.01)
        new_content = "def hello():\n    return 'changed'\n# extra line\n"
        tmp_file.write_text(new_content)
        # Bump mtime past resolution-limited filesystems explicitly.
        future = time.time() + 1
        os.utime(tmp_file, (future, future))

        cache.bump_turn()
        second = _read(tool, tmp_file)
        assert second["success"] is True
        assert second.get("cached") is not True
        assert "changed" in second["content"]

    def test_partial_read_bypasses_cache(self, tmp_file):
        cache = FileReadCache()
        cache.bump_turn()
        tool = ReadFileTool()
        tool.read_cache = cache

        first = _read(tool, tmp_file, start_line=1, end_line=1)
        assert first.get("cached") is not True
        assert "def hello" in first["content"]

        cache.bump_turn()
        # A partial read does NOT seed the cache; full re-read therefore
        # also returns full content (not a placeholder).
        second = _read(tool, tmp_file)
        assert second["success"] is True
        assert second.get("cached") is not True
        assert "hello" in second["content"]

        # And a partial read after a full read also bypasses (no placeholder).
        cache.bump_turn()
        third = _read(tool, tmp_file, start_line=1, end_line=1)
        assert third.get("cached") is not True
        assert "def hello" in third["content"]

    def test_no_cache_attached_works_normally(self, tmp_file):
        # Backwards compat: if no cache is wired, the tool reads normally.
        tool = ReadFileTool()
        tool.read_cache = None
        result = _read(tool, tmp_file)
        assert result["success"] is True
        assert result.get("cached") is not True
        assert "hello" in result["content"]
