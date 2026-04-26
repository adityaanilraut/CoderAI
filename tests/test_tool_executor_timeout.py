import asyncio
import pytest

from coderAI.tool_executor import ToolExecutor, DEFAULT_TOOL_TIMEOUT_SECONDS
from coderAI.tools.base import Tool, ToolRegistry
from unittest.mock import MagicMock, AsyncMock

class FastTool(Tool):
    name = "fast_tool"
    is_read_only = True

    async def execute(self, **kwargs):
        return {"success": True, "output": "fast"}

class SlowTool(Tool):
    name = "slow_tool"
    is_read_only = True
    timeout = 0.1

    async def execute(self, **kwargs):
        await asyncio.sleep(5)
        return {"success": True, "output": "slow"}

@pytest.mark.asyncio
async def test_fast_tool():
    agent = MagicMock()
    agent.auto_approve = True
    registry = ToolRegistry()
    registry.register(FastTool())
    agent.tools = registry
    executor = ToolExecutor(agent)
    
    pc = {
        "tool_id": "1",
        "tool_name": "fast_tool",
        "arguments": {}
    }
    hooks_manager = AsyncMock()
    hooks_manager.run_hooks.return_value = None
    
    result = await executor.execute_single_tool(pc, None, hooks_manager)
    assert result == {"success": True, "output": "fast"}

@pytest.mark.asyncio
async def test_slow_tool_timeout():
    agent = MagicMock()
    agent.auto_approve = True
    registry = ToolRegistry()
    registry.register(SlowTool())
    agent.tools = registry
    executor = ToolExecutor(agent)
    
    pc = {
        "tool_id": "2",
        "tool_name": "slow_tool",
        "arguments": {}
    }
    hooks_manager = AsyncMock()
    hooks_manager.run_hooks.return_value = None
    
    result = await executor.execute_single_tool(pc, None, hooks_manager)
    assert result == {
        "success": False,
        "error": "Tool 'slow_tool' exceeded timeout of 0.1s",
        "error_code": "timeout",
    }

@pytest.mark.asyncio
async def test_module_default_timeout():
    class ModuleSlowTool(Tool):
        name = "module_slow"
        is_read_only = True

        async def execute(self, **kwargs):
            await asyncio.sleep(5)
            return {"success": True}

    agent = MagicMock()
    agent.auto_approve = True
    registry = ToolRegistry()
    registry.register(ModuleSlowTool())
    agent.tools = registry
    executor = ToolExecutor(agent)
    
    # We monkeypatch the DEFAULT_TOOL_TIMEOUT_SECONDS for testing
    import coderAI.tool_executor
    old_default = coderAI.tool_executor.DEFAULT_TOOL_TIMEOUT_SECONDS
    coderAI.tool_executor.DEFAULT_TOOL_TIMEOUT_SECONDS = 0.1
    
    try:
        pc = {
            "tool_id": "3",
            "tool_name": "module_slow",
            "arguments": {}
        }
        hooks_manager = AsyncMock()
        hooks_manager.run_hooks.return_value = None
        
        result = await executor.execute_single_tool(pc, None, hooks_manager)
        assert result == {
            "success": False,
            "error": "Tool 'module_slow' exceeded timeout of 0.1s",
            "error_code": "timeout",
        }
    finally:
        coderAI.tool_executor.DEFAULT_TOOL_TIMEOUT_SECONDS = old_default
