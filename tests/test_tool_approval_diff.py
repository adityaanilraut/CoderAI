import pytest
from coderAI.tool_executor import ToolExecutor
from unittest.mock import MagicMock

def test_compute_preview_diff_write_file(tmp_path):
    # Setup
    f = tmp_path / "test.txt"
    f.write_text("hello\n", encoding="utf-8")
    
    agent = MagicMock()
    executor = ToolExecutor(agent)
    
    # Test overwrite
    diff = executor._compute_preview_diff("write_file", {"path": str(f), "content": "world\n"})
    assert "-hello" in diff
    assert "+world" in diff
    
    # Test append
    diff2 = executor._compute_preview_diff("write_file", {"path": str(f), "content": "world\n", "append": True})
    assert " hello" in diff2
    assert "+world" in diff2

def test_compute_preview_diff_search_replace(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("hello world\n", encoding="utf-8")
    
    executor = ToolExecutor(MagicMock())
    
    diff = executor._compute_preview_diff("search_replace", {"path": str(f), "search": "world", "replace": "there"})
    assert "-hello world" in diff
    assert "+hello there" in diff

def test_compute_preview_diff_apply_diff(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("hello\n", encoding="utf-8")
    executor = ToolExecutor(MagicMock())
    
    # apply_diff just returns the passed diff
    raw_diff = "--- a/test.txt\n+++ b/test.txt\n@@ -1 +1 @@\n-hello\n+world"
    diff = executor._compute_preview_diff("apply_diff", {"path": str(f), "diff": raw_diff})
    assert diff == raw_diff

def test_compute_preview_diff_multi_edit(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("a\nb\n", encoding="utf-8")
    
    executor = ToolExecutor(MagicMock())
    
    diff = executor._compute_preview_diff("multi_edit", {
        "path": str(f), 
        "edits": [
            {"search": "a", "replace": "A"},
            {"search": "b", "replace": "B"}
        ]
    })
    
    assert "-a" in diff
    assert "+A" in diff
    assert "-b" in diff
    assert "+B" in diff

def test_compute_preview_diff_truncation(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("a\n", encoding="utf-8")
    
    executor = ToolExecutor(MagicMock())
    
    large_content = "X" * 40000
    diff = executor._compute_preview_diff("write_file", {"path": str(f), "content": large_content})
    
    assert len(diff) <= 32768 + 50 # 32768 + length of truncation message
    assert "(diff truncated)" in diff
