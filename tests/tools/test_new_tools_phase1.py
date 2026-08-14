"""Tests for newly implemented Phase 1 tools:
- WriteBgInputTool
- DirectoryTreeTool
- ReadFileSliceTool
"""

import asyncio
import sys
from pathlib import Path
import pytest

from coderAI.tools.terminal import (
    RunBackgroundTool,
    ReadBgOutputTool,
    KillProcessTool,
    WriteBgInputTool,
    _tracked_bg_processes,
)
from coderAI.tools.filesystem.manage import DirectoryTreeTool
from coderAI.tools.filesystem.read_write import ReadFileSliceTool


@pytest.mark.asyncio
async def test_write_bg_input_and_read_output(tmp_path: Path):
    bg_tool = RunBackgroundTool()
    write_tool = WriteBgInputTool()
    read_tool = ReadBgOutputTool()
    kill_tool = KillProcessTool()

    # Launch a python script that reads lines from stdin and echoes them
    cmd = (
        sys.executable
        + " -u -c \"import sys; [print('ECHO: ' + line.strip(), flush=True) for line in sys.stdin]\""
    )
    res = await bg_tool.execute(command=cmd, working_dir=str(tmp_path), capture_output=True)
    assert res["success"] is True
    pid = res["pid"]

    try:
        # Give process a moment to start
        await asyncio.sleep(0.1)

        # Send input
        w_res = await write_tool.execute(pid=pid, input="hello world\n")
        assert w_res["success"] is True
        assert w_res["bytes_written"] == len("hello world\n")

        # Wait for echo
        await asyncio.sleep(0.2)
        out_res = await read_tool.execute(pid=pid)
        assert out_res["success"] is True
        assert "ECHO: hello world" in out_res["stdout"]

        # Send another line and close stdin with eof=True
        w_res2 = await write_tool.execute(pid=pid, input="second line\n", eof=True)
        assert w_res2["success"] is True
        assert w_res2["closed_stdin"] is True

        # Process should terminate cleanly after EOF on stdin
        await asyncio.sleep(0.3)
        out_res2 = await read_tool.execute(pid=pid)
        assert out_res2["success"] is True
        assert "ECHO: second line" in out_res2["stdout"]
    finally:
        if pid in _tracked_bg_processes:
            await kill_tool.execute(pid=pid, force=True)


@pytest.mark.asyncio
async def test_write_bg_input_invalid_pid():
    write_tool = WriteBgInputTool()
    res = await write_tool.execute(pid=99999999, input="test\n")
    assert res["success"] is False
    assert "No tracked background process" in res["error"]


@pytest.mark.asyncio
async def test_directory_tree_tool(tmp_path: Path):
    # Setup a sample directory tree
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('main')")
    (tmp_path / "src" / "utils").mkdir()
    (tmp_path / "src" / "utils" / "helper.py").write_text("# helper")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_main.py").write_text("# test")
    (tmp_path / ".hidden_dir").mkdir()
    (tmp_path / ".hidden_dir" / "secret.txt").write_text("shh")
    (tmp_path / "README.md").write_text("# Readme")

    tree_tool = DirectoryTreeTool()

    # Normal tree without hidden files
    res = await tree_tool.execute(path=str(tmp_path), max_depth=3, include_hidden=False)
    assert res["success"] is True
    tree_text = res["tree"]
    assert "src/" in tree_text
    assert "main.py" in tree_text
    assert "utils/" in tree_text
    assert "helper.py" in tree_text
    assert "tests/" in tree_text
    assert "README.md" in tree_text
    assert ".hidden_dir" not in tree_text
    assert res["total_directories"] >= 3
    assert res["total_files"] >= 4

    # Tree with hidden files
    res_hidden = await tree_tool.execute(path=str(tmp_path), max_depth=3, include_hidden=True)
    assert res_hidden["success"] is True
    assert ".hidden_dir/" in res_hidden["tree"]


@pytest.mark.asyncio
async def test_directory_tree_not_found(tmp_path: Path):
    tree_tool = DirectoryTreeTool()
    res = await tree_tool.execute(path=str(tmp_path / "non_existent"))
    assert res["success"] is False
    assert "Directory not found" in res["error"]


@pytest.mark.asyncio
async def test_read_file_slice_tool(tmp_path: Path):
    sample_file = tmp_path / "large_file.txt"
    lines = [f"Line {i}" for i in range(1, 101)]
    sample_file.write_text("\n".join(lines))

    slice_tool = ReadFileSliceTool()

    # Read first 10 lines without line numbers
    res1 = await slice_tool.execute(
        path=str(sample_file), offset=1, limit=10, with_line_numbers=False
    )
    assert res1["success"] is True
    assert res1["start_line"] == 1
    assert res1["end_line"] == 10
    assert res1["line_count"] == 10
    assert res1["has_more"] is True
    assert res1["next_offset"] == 11
    assert res1["content"] == "\n".join(lines[:10])

    # Read next 10 lines with line numbers
    res2 = await slice_tool.execute(
        path=str(sample_file), offset=11, limit=10, with_line_numbers=True
    )
    assert res2["success"] is True
    assert res2["start_line"] == 11
    assert res2["end_line"] == 20
    assert "  11: Line 11" in res2["content"]
    assert "  20: Line 20" in res2["content"]

    # Read to end
    res3 = await slice_tool.execute(
        path=str(sample_file), offset=91, limit=20, with_line_numbers=False
    )
    assert res3["success"] is True
    assert res3["start_line"] == 91
    assert res3["end_line"] == 100
    assert res3["line_count"] == 10
    assert res3["has_more"] is False
    assert res3["next_offset"] is None


@pytest.mark.asyncio
async def test_read_file_slice_validation(tmp_path: Path):
    slice_tool = ReadFileSliceTool()
    sample_file = tmp_path / "test.txt"
    sample_file.write_text("abc")

    res_err1 = await slice_tool.execute(path=str(sample_file), offset=0)
    assert res_err1["success"] is False
    assert "offset must be at least 1" in res_err1["error"]

    res_err2 = await slice_tool.execute(path=str(sample_file), offset=1, limit=0)
    assert res_err2["success"] is False
    assert "limit must be at least 1" in res_err2["error"]
