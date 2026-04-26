import asyncio
import os
import pytest
from coderAI.tools.multi_edit import MultiEditTool

@pytest.fixture
def temp_file(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("line1\nline2\nline3\n", encoding="utf-8")
    return f

def test_multi_edit_success(temp_file):
    tool = MultiEditTool()
    edits = [
        {"search": "line1\n", "replace": "LINE1\n", "expected_count": 1},
        {"search": "line3", "replace": "LINE3", "expected_count": 1}
    ]
    result = asyncio.run(tool.execute(path=str(temp_file), edits=edits))
    
    assert result["success"] is True
    assert result["edits_applied"] == 2
    content = temp_file.read_text(encoding="utf-8")
    assert content == "LINE1\nline2\nLINE3\n"

def test_multi_edit_count_mismatch(temp_file):
    tool = MultiEditTool()
    edits = [
        {"search": "line2\n", "replace": "LINE2\n", "expected_count": 2}
    ]
    result = asyncio.run(tool.execute(path=str(temp_file), edits=edits))
    
    assert result["success"] is True
    assert result["count_mismatches"][0]["expected_count"] == 2
    assert result["count_mismatches"][0]["actual_count"] == 1
    assert temp_file.read_text(encoding="utf-8") == "line1\nLINE2\nline3\n"

def test_multi_edit_atomic_write_error(tmp_path, monkeypatch):
    f = tmp_path / "test.txt"
    f.write_text("hello", encoding="utf-8")
    
    tool = MultiEditTool()
    edits = [{"search": "hello", "replace": "world", "expected_count": 1}]
    
    def fake_replace(src, dst):
        raise OSError("Permission denied")
    
    monkeypatch.setattr(os, "replace", fake_replace)
    
    result = asyncio.run(tool.execute(path=str(f), edits=edits))
    assert result["success"] is False
    assert "Permission denied" in result["error"]
    
    # Original file is unchanged
    assert f.read_text(encoding="utf-8") == "hello"
