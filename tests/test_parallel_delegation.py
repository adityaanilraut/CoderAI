"""Tests for parallel mutating sub-agent delegation and resource isolation."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from coderAI.core.execution_context import (
    execution_context_scope,
    get_execution_context,
    resolve_delegation_isolation_domain,
)
from coderAI.core.tool_executor import ToolExecutor
from coderAI.tools.subagent import DelegateTaskTool


def _make_agent(tool_registry, *, max_mutating: int = 3, auto_approve=True):
    config = SimpleNamespace(max_concurrent_mutating_subagents=max_mutating)
    agent = SimpleNamespace(
        auto_approve=auto_approve,
        ipc_server=None,
        tools=tool_registry,
        tracker_info=None,
        config=config,
        _sync_tracker=MagicMock(),
        _tool_approval_allowlist=set(),
        max_retries_per_tool=0,
    )
    return agent


def _make_registry(tool_map):
    registry = SimpleNamespace()

    def _get(name):
        return tool_map.get(name)

    async def _execute(name, **kwargs):
        tool = tool_map.get(name)
        if tool is None:
            raise ValueError(f"Tool not found: {name}")
        return await tool.execute(**kwargs)

    registry.get = MagicMock(side_effect=_get)
    registry.execute = AsyncMock(side_effect=_execute)
    return registry


class TestResolveDelegationIsolationDomain:
    def test_read_only_task(self):
        assert resolve_delegation_isolation_domain({"read_only_task": True}) == "read_only"

    def test_browser_domain(self):
        assert resolve_delegation_isolation_domain({"isolation_domain": "browser"}) == "browser"

    def test_desktop_domain(self):
        assert resolve_delegation_isolation_domain({"isolation_domain": "desktop"}) == "desktop"

    def test_auto_defaults_to_workspace(self):
        assert resolve_delegation_isolation_domain({"isolation_domain": "auto"}) == "workspace"

    def test_missing_args_defaults_to_workspace(self):
        assert resolve_delegation_isolation_domain(None) == "workspace"


class TestExecutionContext:
    def test_scope_sets_agent_id(self):
        with execution_context_scope("agent-123", isolation_domain="browser"):
            ctx = get_execution_context()
            assert ctx.agent_id == "agent-123"
            assert ctx.isolation_domain == "browser"
        assert get_execution_context().agent_id == "main"


class TestDelegateTaskToolRoutingMetadata:
    def test_not_marked_read_only(self):
        assert DelegateTaskTool.is_read_only is False

    def test_max_parallel_zero_uses_domain_scheduler(self):
        assert DelegateTaskTool.max_parallel_invocations == 0

    def test_isolation_domain_in_schema(self):
        tool = DelegateTaskTool()
        schema = tool.parameters_model.model_json_schema()
        assert "isolation_domain" in schema.get("properties", {})


class TestBrowserRegistryIsolation:
    @pytest.mark.asyncio
    async def test_per_agent_sessions_are_distinct(self):
        from coderAI.tools.browser import BrowserRegistry, BrowserSession

        BrowserRegistry.reset_for_tests()
        registry = BrowserRegistry.get()

        session_a = await registry.for_agent("agent-a")
        session_b = await registry.for_agent("agent-b")

        assert isinstance(session_a, BrowserSession)
        assert isinstance(session_b, BrowserSession)
        assert session_a is not session_b
        assert session_a.agent_id == "agent-a"
        assert session_b.agent_id == "agent-b"

        await registry.close_agent("agent-a")
        await registry.close_agent("agent-b")
        BrowserRegistry.reset_for_tests()

    @pytest.mark.asyncio
    async def test_execution_context_selects_session(self):
        from coderAI.tools.browser import BrowserRegistry

        BrowserRegistry.reset_for_tests()
        registry = BrowserRegistry.get()

        with execution_context_scope("ctx-agent", isolation_domain="browser"):
            session = await registry.for_agent()
            assert session.agent_id == "ctx-agent"

        BrowserRegistry.reset_for_tests()


class TestParallelMutatingDelegationBatch:
    @pytest.mark.asyncio
    async def test_browser_delegates_run_in_parallel(self):
        started: list[int] = []
        finished: list[int] = []

        async def _slow_delegate(**kwargs):
            idx = kwargs.get("_idx")
            started.append(idx)
            await asyncio.sleep(0.15)
            finished.append(idx)
            return {"success": True, "final_report": f"done-{idx}"}

        delegate_tool = SimpleNamespace(
            name="delegate_task",
            is_read_only=False,
            max_parallel_invocations=0,
            requires_confirmation=False,
            execute=AsyncMock(side_effect=_slow_delegate),
        )
        registry = _make_registry({"delegate_task": delegate_tool})
        agent = _make_agent(registry)
        executor = ToolExecutor(agent)
        hooks_manager = AsyncMock()
        hooks_manager.run_hooks.return_value = None

        parsed = [
            {
                "tool_id": "1",
                "tool_name": "delegate_task",
                "arguments": {
                    "task_description": "browser task a",
                    "isolation_domain": "browser",
                    "_idx": 1,
                },
            },
            {
                "tool_id": "2",
                "tool_name": "delegate_task",
                "arguments": {
                    "task_description": "browser task b",
                    "isolation_domain": "browser",
                    "_idx": 2,
                },
            },
        ]

        t0 = time.monotonic()
        results = await executor.run_tool_batch(parsed, None, hooks_manager)
        elapsed = time.monotonic() - t0

        assert all(r["success"] for r in results)
        assert set(started) == {1, 2}
        assert set(finished) == {1, 2}
        assert elapsed < 0.28

    @pytest.mark.asyncio
    async def test_desktop_delegates_run_serially(self):
        order: list[int] = []

        async def _numbered_delegate(**kwargs):
            idx = kwargs.get("_idx")
            order.append(idx)
            await asyncio.sleep(0.05)
            order.append(idx)
            return {"success": True, "final_report": "ok"}

        delegate_tool = SimpleNamespace(
            name="delegate_task",
            is_read_only=False,
            max_parallel_invocations=0,
            requires_confirmation=False,
            execute=AsyncMock(side_effect=_numbered_delegate),
        )
        registry = _make_registry({"delegate_task": delegate_tool})
        agent = _make_agent(registry)
        executor = ToolExecutor(agent)
        hooks_manager = AsyncMock()
        hooks_manager.run_hooks.return_value = None

        parsed = [
            {
                "tool_id": "1",
                "tool_name": "delegate_task",
                "arguments": {
                    "task_description": "desktop a",
                    "isolation_domain": "desktop",
                    "_idx": 1,
                },
            },
            {
                "tool_id": "2",
                "tool_name": "delegate_task",
                "arguments": {
                    "task_description": "desktop b",
                    "isolation_domain": "desktop",
                    "_idx": 2,
                },
            },
        ]

        await executor.run_tool_batch(parsed, None, hooks_manager)
        assert order == [1, 1, 2, 2]

    @pytest.mark.asyncio
    async def test_workspace_delegates_run_serially(self):
        order: list[int] = []

        async def _numbered_delegate(**kwargs):
            idx = kwargs.get("_idx")
            order.append(idx)
            await asyncio.sleep(0.05)
            order.append(idx)
            return {"success": True, "final_report": "ok"}

        delegate_tool = SimpleNamespace(
            name="delegate_task",
            is_read_only=False,
            max_parallel_invocations=0,
            requires_confirmation=False,
            execute=AsyncMock(side_effect=_numbered_delegate),
        )
        registry = _make_registry({"delegate_task": delegate_tool})
        agent = _make_agent(registry)
        executor = ToolExecutor(agent)
        hooks_manager = AsyncMock()
        hooks_manager.run_hooks.return_value = None

        parsed = [
            {
                "tool_id": "1",
                "tool_name": "delegate_task",
                "arguments": {"task_description": "a", "isolation_domain": "workspace", "_idx": 1},
            },
            {
                "tool_id": "2",
                "tool_name": "delegate_task",
                "arguments": {"task_description": "b", "isolation_domain": "auto", "_idx": 2},
            },
        ]

        await executor.run_tool_batch(parsed, None, hooks_manager)
        assert order == [1, 1, 2, 2]

    @pytest.mark.asyncio
    async def test_mutating_cap_limits_parallelism(self):
        concurrent = 0
        peak = 0
        lock = asyncio.Lock()

        async def _delegate(**kwargs):
            nonlocal concurrent, peak
            async with lock:
                concurrent += 1
                peak = max(peak, concurrent)
            await asyncio.sleep(0.1)
            async with lock:
                concurrent -= 1
            return {"success": True, "final_report": "ok"}

        delegate_tool = SimpleNamespace(
            name="delegate_task",
            is_read_only=False,
            max_parallel_invocations=0,
            requires_confirmation=False,
            execute=AsyncMock(side_effect=_delegate),
        )
        registry = _make_registry({"delegate_task": delegate_tool})
        agent = _make_agent(registry, max_mutating=2)
        executor = ToolExecutor(agent)
        hooks_manager = AsyncMock()
        hooks_manager.run_hooks.return_value = None

        parsed = [
            {
                "tool_id": str(i),
                "tool_name": "delegate_task",
                "arguments": {
                    "task_description": f"task-{i}",
                    "isolation_domain": "browser",
                },
            }
            for i in range(4)
        ]

        await executor.run_tool_batch(parsed, None, hooks_manager)
        assert peak <= 2
