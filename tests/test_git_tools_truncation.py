import asyncio
import pytest
from unittest.mock import AsyncMock, patch

from coderAI.tools.git import (
    GitDiffTool,
    GitShowTool,
    GitStatusTool,
    GitLogTool,
    GitBranchTool,
    GitBlameTool,
    MAX_GIT_OUTPUT_BYTES,
)

@pytest.fixture
def mock_subprocess():
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        process_mock = AsyncMock()
        process_mock.returncode = 0
        mock_exec.return_value = process_mock
        yield mock_exec, process_mock

@pytest.mark.asyncio
async def test_git_diff_truncation_small(mock_subprocess):
    mock_exec, process_mock = mock_subprocess
    small_output = b"diff --git a/file.txt b/file.txt\n"
    process_mock.communicate.return_value = (small_output, b"")
    
    tool = GitDiffTool()
    result = await tool.execute()
    
    assert result["success"] is True
    assert result["truncated"] is False
    assert result["diff"] == small_output.decode("utf-8")

@pytest.mark.asyncio
async def test_git_diff_truncation_large(mock_subprocess):
    mock_exec, process_mock = mock_subprocess
    # Create 200KB of output
    large_output = b"a" * 200_000
    process_mock.communicate.return_value = (large_output, b"")
    
    tool = GitDiffTool()
    result = await tool.execute()
    
    assert result["success"] is True
    assert result["truncated"] is True
    assert "[... truncated," in result["diff"]
    # The output should be max bytes + the truncation marker length
    assert len(result["diff"]) < 100_000

@pytest.mark.asyncio
async def test_git_status_truncation_large(mock_subprocess):
    mock_exec, process_mock = mock_subprocess
    large_output = b" M file.txt\n" * 10_000 # ~120KB
    process_mock.communicate.return_value = (large_output, b"")
    
    tool = GitStatusTool()
    result = await tool.execute()
    
    assert result["success"] is True
    assert result["truncated"] is True
    assert "[... truncated," in result["status"]

@pytest.mark.asyncio
async def test_git_log_truncation_large(mock_subprocess):
    mock_exec, process_mock = mock_subprocess
    large_output = b"commit hash\n" * 10_000 # ~120KB
    process_mock.communicate.return_value = (large_output, b"")
    
    tool = GitLogTool()
    result = await tool.execute(limit=10000)
    
    assert result["success"] is True
    assert result["truncated"] is True
    assert "[... truncated," in result["log"]

@pytest.mark.asyncio
async def test_git_branch_truncation_large(mock_subprocess):
    mock_exec, process_mock = mock_subprocess
    large_output = b"  branch_name\n" * 10_000 # ~140KB
    process_mock.communicate.return_value = (large_output, b"")
    
    tool = GitBranchTool()
    result = await tool.execute(action="list")
    
    assert result["success"] is True
    assert result["truncated"] is True
    
@pytest.mark.asyncio
async def test_git_show_truncation_large(mock_subprocess):
    mock_exec, process_mock = mock_subprocess
    large_output = b"diff data\n" * 10_000 # ~100KB
    process_mock.communicate.return_value = (large_output, b"")
    
    tool = GitShowTool()
    result = await tool.execute()
    
    assert result["success"] is True
    assert result["truncated"] is True
    assert "[... truncated," in result["output"]

@pytest.mark.asyncio
async def test_git_blame_truncation_large(mock_subprocess):
    mock_exec, process_mock = mock_subprocess
    large_output = b"blame info\n" * 10_000 # ~110KB
    process_mock.communicate.return_value = (large_output, b"")
    
    tool = GitBlameTool()
    result = await tool.execute(file_path="huge.txt")
    
    assert result["success"] is True
    assert result["truncated"] is True
