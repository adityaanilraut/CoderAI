import asyncio
from types import SimpleNamespace

import pytest

from coderAI.core.services import services_scope
from coderAI.core.tool_executor import ToolExecutor, resolve_tool_timeout
from coderAI.system.config import Config
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
        await asyncio.sleep(0.3)
        return {"success": True, "output": "slow"}


@pytest.mark.asyncio
async def test_fast_tool():
    agent = MagicMock()
    agent.auto_approve = True
    registry = ToolRegistry()
    registry.register(FastTool())
    agent.tools = registry
    executor = ToolExecutor(agent)

    pc = {"tool_id": "1", "tool_name": "fast_tool", "arguments": {}}
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

    pc = {"tool_id": "2", "tool_name": "slow_tool", "arguments": {}}
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
            await asyncio.sleep(0.3)
            return {"success": True}

    agent = MagicMock()
    agent.auto_approve = True
    registry = ToolRegistry()
    registry.register(ModuleSlowTool())
    agent.tools = registry
    executor = ToolExecutor(agent)

    # We monkeypatch the DEFAULT_TOOL_TIMEOUT_SECONDS for testing
    import coderAI.core.tool_executor

    old_default = coderAI.core.tool_executor.DEFAULT_TOOL_TIMEOUT_SECONDS
    coderAI.core.tool_executor.DEFAULT_TOOL_TIMEOUT_SECONDS = 0.1

    try:
        pc = {"tool_id": "3", "tool_name": "module_slow", "arguments": {}}
        hooks_manager = AsyncMock()
        hooks_manager.run_hooks.return_value = None

        result = await executor.execute_single_tool(pc, None, hooks_manager)
        assert result == {
            "success": False,
            "error": "Tool 'module_slow' exceeded timeout of 0.1s",
            "error_code": "timeout",
        }
    finally:
        coderAI.core.tool_executor.DEFAULT_TOOL_TIMEOUT_SECONDS = old_default


# ── resolve_tool_timeout precedence ─────────────────────────────────────
#
# Precedence (first hit wins):
#   1. tool.resolve_timeout(arguments)      — argument-derived cap
#   2. config.tool_timeout_overrides[name]  — per-tool config override
#   3. tool.timeout class attribute
#   4. config.tool_timeout_seconds          — only when explicitly set
#   5. DEFAULT_TOOL_TIMEOUT_SECONDS         — live-read module default


class ArgDerivedTool(Tool):
    name = "arg_derived"
    is_read_only = True
    timeout = 10.0

    def resolve_timeout(self, arguments):
        return 42.0

    async def execute(self, **kwargs):
        return {"success": True}


class ClassAttrTool(Tool):
    name = "class_attr"
    is_read_only = True
    timeout = 10.0

    async def execute(self, **kwargs):
        return {"success": True}


class BareTool(Tool):
    name = "bare"
    is_read_only = True

    async def execute(self, **kwargs):
        return {"success": True}


class TestResolveToolTimeoutPrecedence:
    def test_level1_resolve_timeout_wins_over_everything(self):
        cfg = Config(tool_timeout_overrides={"arg_derived": 20.0}, tool_timeout_seconds=30.0)
        with services_scope(config=cfg):
            assert resolve_tool_timeout(ArgDerivedTool(), "arg_derived", {}) == 42.0

    def test_level2_config_override_wins_over_class_attr(self):
        cfg = Config(tool_timeout_overrides={"class_attr": 20.0}, tool_timeout_seconds=30.0)
        with services_scope(config=cfg):
            assert resolve_tool_timeout(ClassAttrTool(), "class_attr", {}) == 20.0

    def test_level3_class_attr_wins_over_config_default(self):
        with services_scope(config=Config(tool_timeout_seconds=30.0)):
            assert resolve_tool_timeout(ClassAttrTool(), "class_attr", {}) == 10.0

    def test_level4_explicit_config_value_wins_over_module_default(self):
        with services_scope(config=Config(tool_timeout_seconds=30.0)):
            assert resolve_tool_timeout(BareTool(), "bare", {}) == 30.0

    def test_level5_module_default_when_config_not_explicit(self, monkeypatch):
        # Config() leaves tool_timeout_seconds at its pydantic default (not in
        # model_fields_set), so the live-read module default must apply.
        import coderAI.core.tool_executor as te

        monkeypatch.setattr(te, "DEFAULT_TOOL_TIMEOUT_SECONDS", 7.5)
        with services_scope(config=Config()):
            assert resolve_tool_timeout(BareTool(), "bare", {}) == 7.5

    def test_broken_resolve_timeout_degrades_to_next_level(self):
        class BrokenResolveTool(ClassAttrTool):
            name = "broken_resolve"

            def resolve_timeout(self, arguments):
                raise RuntimeError("boom")

        with services_scope(config=Config()):
            assert resolve_tool_timeout(BrokenResolveTool(), "broken_resolve", {}) == 10.0

    def test_simplenamespace_tool_mock_supported(self, monkeypatch):
        # Executor tests drive SimpleNamespace stand-ins; attr access must
        # stay defensive.
        import coderAI.core.tool_executor as te

        monkeypatch.setattr(te, "DEFAULT_TOOL_TIMEOUT_SECONDS", 9.0)
        mock_tool = SimpleNamespace(timeout=None)
        with services_scope(config=Config()):
            assert resolve_tool_timeout(mock_tool, "mock", {}) == 9.0


@pytest.mark.asyncio
async def test_resolve_timeout_drives_executor_timeout():
    """A tool's argument-derived cap (not its class attr) kills the call."""

    class ArgSlowTool(Tool):
        name = "arg_slow"
        is_read_only = True
        timeout = 60.0  # class attr would never fire in this test

        def resolve_timeout(self, arguments):
            return 0.1

        async def execute(self, **kwargs):
            await asyncio.sleep(0.3)
            return {"success": True}

    agent = MagicMock()
    agent.auto_approve = True
    registry = ToolRegistry()
    registry.register(ArgSlowTool())
    agent.tools = registry
    executor = ToolExecutor(agent)

    pc = {"tool_id": "4", "tool_name": "arg_slow", "arguments": {}}
    hooks_manager = AsyncMock()
    hooks_manager.run_hooks.return_value = None

    result = await executor.execute_single_tool(pc, None, hooks_manager)
    assert result == {
        "success": False,
        "error": "Tool 'arg_slow' exceeded timeout of 0.1s",
        "error_code": "timeout",
    }
