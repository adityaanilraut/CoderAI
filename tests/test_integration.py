import pytest
import os
import shutil
from pathlib import Path

from coderAI.tools import ToolRegistry
from coderAI.tools.filesystem import ReadFileTool, WriteFileTool, SearchReplaceTool, ListDirectoryTool
from coderAI.tools.terminal import RunCommandTool


@pytest.fixture
def sandbox_dir(tmp_path):
    """Create a temporary sandbox directory for testing."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    
    # Change current working directory to the sandbox
    original_cwd = os.getcwd()
    os.chdir(sandbox)
    
    yield sandbox
    
    # Restore original working directory
    os.chdir(original_cwd)
    shutil.rmtree(sandbox, ignore_errors=True)


@pytest.fixture
def tool_registry():
    """Create a ToolRegistry with the necessary tools for testing."""
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(SearchReplaceTool())
    registry.register(ListDirectoryTool())
    registry.register(RunCommandTool())
    return registry


@pytest.mark.asyncio
async def test_filesystem_tools_integration(sandbox_dir, tool_registry):
    """End-to-end integration test of filesystem tools."""
    test_file = sandbox_dir / "test.txt"
    content = "Hello, Integration World!"
    
    # Test WriteFileTool
    write_result = await tool_registry.execute("write_file", path=str(test_file), content=content)
    assert write_result["success"] is True
    assert test_file.exists()
    assert test_file.read_text() == content
    
    # Test ReadFileTool
    read_result = await tool_registry.execute("read_file", path=str(test_file))
    assert read_result["success"] is True
    assert read_result["content"] == content
    
    # Test SearchReplaceTool
    replace_result = await tool_registry.execute(
        "search_replace", 
        path=str(test_file), 
        search="World", 
        replace="Testing"
    )
    assert replace_result["success"] is True
    assert test_file.read_text() == "Hello, Integration Testing!"
    
    # Test ListDirectoryTool
    list_result = await tool_registry.execute("list_directory", path=str(sandbox_dir))
    assert list_result["success"] is True
    assert len(list_result["entries"]) == 1
    assert list_result["entries"][0]["name"] == "test.txt"


@pytest.mark.asyncio
async def test_terminal_tool_integration(sandbox_dir, tool_registry):
    """Integration test for the terminal tool."""
    cmd_result = await tool_registry.execute("run_command", command="echo 'integration testing'", working_dir=str(sandbox_dir))
    assert cmd_result["success"] is True
    assert "integration testing" in cmd_result["stdout"]


@pytest.mark.asyncio
async def test_pydantic_validation_error(tool_registry):
    """Verify that calling a tool with missing args results in a friendly Pydantic validation error."""
    # WriteFileTool requires 'path' and 'content'
    result = await tool_registry.execute("write_file", content="missing path")
    assert result["success"] is False
    assert result["error_code"] == "validation_error"
    assert "validation error" in result["error"].lower()
