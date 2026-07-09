"""Batch (``edits=``) editing via SearchReplaceTool.

The deprecated ``multi_edit`` alias was removed; these cover the batch path it
used to delegate to — ``SearchReplaceTool.execute(path=..., edits=[...])`` —
including its atomic-write and registry-dispatch behavior.
"""

import asyncio
import os

import pytest

from coderAI.tools.filesystem.edit import SearchReplaceTool


@pytest.fixture
def temp_file(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("line1\nline2\nline3\n", encoding="utf-8")
    return f


def test_batch_edit_success(temp_file):
    tool = SearchReplaceTool()
    edits = [
        {"search": "line1\n", "replace": "LINE1\n", "expected_count": 1},
        {"search": "line3", "replace": "LINE3", "expected_count": 1},
    ]
    result = asyncio.run(tool.execute(path=str(temp_file), edits=edits))

    assert result["success"] is True
    assert result["edits_applied"] == 2
    content = temp_file.read_text(encoding="utf-8")
    assert content == "LINE1\nline2\nLINE3\n"


def test_batch_edit_count_mismatch(temp_file):
    tool = SearchReplaceTool()
    edits = [{"search": "line2\n", "replace": "LINE2\n", "expected_count": 2}]
    result = asyncio.run(tool.execute(path=str(temp_file), edits=edits))

    assert result["success"] is True
    assert result["count_mismatches"][0]["expected_count"] == 2
    assert result["count_mismatches"][0]["actual_count"] == 1
    assert temp_file.read_text(encoding="utf-8") == "line1\nLINE2\nline3\n"


def test_batch_edit_atomic_write_error(tmp_path, monkeypatch):
    f = tmp_path / "test.txt"
    f.write_text("hello", encoding="utf-8")

    tool = SearchReplaceTool()
    edits = [{"search": "hello", "replace": "world", "expected_count": 1}]

    def fake_replace(src, dst):
        raise OSError("Permission denied")

    monkeypatch.setattr(os, "replace", fake_replace)

    result = asyncio.run(tool.execute(path=str(f), edits=edits))
    assert result["success"] is False
    assert "Permission denied" in result["error"]

    # Original file is unchanged
    assert f.read_text(encoding="utf-8") == "hello"


def test_batch_edit_registry_dispatch(tmp_path):
    """search_replace dispatched through ToolRegistry with edits= must not raise."""
    from coderAI.tools.base import ToolRegistry

    f = tmp_path / "test.txt"
    f.write_text("hello world\n", encoding="utf-8")

    registry = ToolRegistry()
    registry.register(SearchReplaceTool())

    result = asyncio.run(
        registry.execute(
            "search_replace",
            path=str(f),
            edits=[{"search": "hello", "replace": "HELLO", "expected_count": 1}],
        )
    )

    assert result["success"] is True
    assert f.read_text(encoding="utf-8") == "HELLO world\n"


def test_batch_edit_empty_search_rejected(tmp_path):
    """Batch-mode edit with empty search text is rejected (not silently corrupt)."""
    f = tmp_path / "test.txt"
    f.write_text("abc\n", encoding="utf-8")

    tool = SearchReplaceTool()
    edits = [{"search": "", "replace": "X"}]
    result = asyncio.run(tool.execute(path=str(f), edits=edits))

    assert result["success"] is False
    assert "search text must be non-empty" in result["error"]


def test_batch_edit_actual_counts_from_modified_content(tmp_path):
    """actual_counts and count_mismatches are computed on sequentially modified content."""
    f = tmp_path / "test.txt"
    f.write_text("hello hello world\n", encoding="utf-8")

    tool = SearchReplaceTool()
    edits = [
        {"search": "hello", "replace": "hi", "expected_count": 2},
        {"search": "hi", "replace": "hey", "expected_count": 2},
    ]
    result = asyncio.run(tool.execute(path=str(f), edits=edits))

    assert result["success"] is True
    assert result["actual_counts"] == [2, 2]
    assert result["count_mismatches"] == []
    assert f.read_text(encoding="utf-8") == "hey hey world\n"
