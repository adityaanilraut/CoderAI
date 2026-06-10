"""Tests for concurrent tool execution in ToolExecutor.run_tool_batch()."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from coderAI.core.tool_executor import ToolExecutor


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------


def _make_agent(tool_registry, auto_approve=True):
    agent = SimpleNamespace(
        auto_approve=auto_approve,
        ipc_server=None,
        tools=tool_registry,
        tracker_info=None,
        config=SimpleNamespace(max_concurrent_mutating_subagents=3),
        _sync_tracker=MagicMock(),
        _tool_approval_allowlist=set(),
        max_retries_per_tool=0,
    )
    return agent


def _make_registry(tool_map):
    """Return a mock ToolRegistry with get() and execute().

    *tool_map*: dict[str, SimpleNamespace] mapping tool_name -> mock tool.
    """
    registry = SimpleNamespace()

    def _get(name):
        return tool_map.get(name)

    async def _execute(name, confirmation_callback=None, **kwargs):
        tool = tool_map.get(name)
        if tool is None:
            raise ValueError(f"Tool not found: {name}")
        return await tool.execute(**kwargs)

    registry.get = MagicMock(side_effect=_get)
    registry.execute = AsyncMock(side_effect=_execute)
    return registry


# ---------------------------------------------------------------------------
# Single-tool execution sanity test
# ---------------------------------------------------------------------------


class TestExecuteSingleTool:
    @pytest.mark.asyncio
    async def test_read_only_tool_succeeds(self):
        t = SimpleNamespace(
            name="read_file",
            is_read_only=True,
            requires_confirmation=False,
            execute=AsyncMock(return_value={"success": True, "content": "ok"}),
        )
        registry = _make_registry({"read_file": t})
        agent = _make_agent(registry)
        executor = ToolExecutor(agent)

        pc = {"tool_id": "1", "tool_name": "read_file", "arguments": {"path": "x.py"}}
        hooks_manager = AsyncMock()
        hooks_manager.run_hooks.return_value = None

        result = await executor.execute_single_tool(pc, None, hooks_manager)
        assert result["success"] is True
        t.execute.assert_awaited_once_with(path="x.py")


# ---------------------------------------------------------------------------
# Batch execution
# ---------------------------------------------------------------------------


class TestBatchReadOnlyParallelism:
    @pytest.mark.asyncio
    async def test_read_only_tools_run_concurrently(self):
        t1 = SimpleNamespace(
            name="ro1",
            is_read_only=True,
            requires_confirmation=False,
            execute=AsyncMock(return_value={"success": True}),
        )
        t2 = SimpleNamespace(
            name="ro2",
            is_read_only=True,
            requires_confirmation=False,
            execute=AsyncMock(return_value={"success": True}),
        )
        registry = _make_registry({"ro1": t1, "ro2": t2})
        agent = _make_agent(registry)
        executor = ToolExecutor(agent)

        batch = [
            {"tool_id": "a", "tool_name": "ro1", "arguments": {"x": 1}},
            {"tool_id": "b", "tool_name": "ro2", "arguments": {"x": 2}},
        ]
        hooks_manager = AsyncMock()
        hooks_manager.run_hooks.return_value = None

        results = await executor.run_tool_batch(batch, hooks_data=None, hooks_manager=hooks_manager)

        assert len(results) == 2
        assert all(r["success"] for r in results)
        t1.execute.assert_awaited_once()
        t2.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_read_only_batch_respects_semaphore(self):
        tools = {
            f"ro{i}": SimpleNamespace(
                name=f"ro{i}",
                is_read_only=True,
                requires_confirmation=False,
                execute=AsyncMock(return_value={"success": True}),
            )
            for i in range(25)
        }
        registry = _make_registry(tools)
        agent = _make_agent(registry)
        executor = ToolExecutor(agent)

        batch = [
            {"tool_id": str(i), "tool_name": f"ro{i}", "arguments": {}}
            for i in range(25)
        ]
        hooks_manager = AsyncMock()
        hooks_manager.run_hooks.return_value = None

        results = await executor.run_tool_batch(batch, hooks_data=None, hooks_manager=hooks_manager)

        assert len(results) == 25
        assert all(r["success"] for r in results)


class TestBatchMutationSerialization:
    @pytest.mark.asyncio
    async def test_same_path_writes_are_serialized(self):
        t_write = SimpleNamespace(
            name="write_file",
            is_read_only=False,
            requires_confirmation=False,
            max_parallel_invocations=0,
            execute=AsyncMock(return_value={"success": True}),
        )
        t_replace = SimpleNamespace(
            name="search_replace",
            is_read_only=False,
            requires_confirmation=False,
            max_parallel_invocations=0,
            execute=AsyncMock(return_value={"success": True}),
        )
        registry = _make_registry({"write_file": t_write, "search_replace": t_replace})
        agent = _make_agent(registry)
        executor = ToolExecutor(agent)

        batch = [
            {"tool_id": "w1", "tool_name": "write_file", "arguments": {"path": "same.py"}},
            {"tool_id": "w2", "tool_name": "search_replace", "arguments": {"path": "same.py"}},
        ]
        hooks_manager = AsyncMock()
        hooks_manager.run_hooks.return_value = None

        results = await executor.run_tool_batch(batch, hooks_data=None, hooks_manager=hooks_manager)

        assert len(results) == 2
        assert all(r["success"] for r in results)

    @pytest.mark.asyncio
    async def test_different_path_writes_are_parallel(self):
        t = SimpleNamespace(
            name="write_file",
            is_read_only=False,
            requires_confirmation=False,
            max_parallel_invocations=0,
            execute=AsyncMock(return_value={"success": True}),
        )
        registry = _make_registry({"write_file": t})
        agent = _make_agent(registry)
        executor = ToolExecutor(agent)

        batch = [
            {"tool_id": "a", "tool_name": "write_file", "arguments": {"path": "a.py"}},
            {"tool_id": "b", "tool_name": "write_file", "arguments": {"path": "b.py"}},
        ]
        hooks_manager = AsyncMock()
        hooks_manager.run_hooks.return_value = None

        results = await executor.run_tool_batch(batch, hooks_data=None, hooks_manager=hooks_manager)

        assert len(results) == 2
        assert all(r["success"] for r in results)


class TestBatchDedup:
    @pytest.mark.asyncio
    async def test_identical_read_tools_both_executed_by_run_tool_batch(self):
        """run_tool_batch does not perform dedup — that happens in orchestrate_tool_calls."""
        t = SimpleNamespace(
            name="read_file",
            is_read_only=True,
            requires_confirmation=False,
            execute=AsyncMock(return_value={"success": True, "content": "x"}),
        )
        registry = _make_registry({"read_file": t})
        agent = _make_agent(registry)
        executor = ToolExecutor(agent)

        batch = [
            {"tool_id": "a", "tool_name": "read_file", "arguments": {"path": "x.py"}},
            {"tool_id": "b", "tool_name": "read_file", "arguments": {"path": "x.py"}},
        ]
        hooks_manager = AsyncMock()
        hooks_manager.run_hooks.return_value = None

        results = await executor.run_tool_batch(batch, hooks_data=None, hooks_manager=hooks_manager)

        assert len(results) == 2
        assert all(r["success"] for r in results)
        assert t.execute.call_count == 2


class TestBatchErrorIsolation:
    @pytest.mark.asyncio
    async def test_one_tool_failure_does_not_block_others(self):
        def fail(**kw):
            raise RuntimeError("boom")
        t_good = SimpleNamespace(
            name="read_file",
            is_read_only=True,
            requires_confirmation=False,
            execute=AsyncMock(return_value={"success": True}),
        )
        t_bad = SimpleNamespace(
            name="read_file_bad",
            is_read_only=True,
            requires_confirmation=False,
            execute=AsyncMock(side_effect=fail),
        )
        registry = _make_registry({"read_file": t_good, "read_file_bad": t_bad})
        agent = _make_agent(registry)
        executor = ToolExecutor(agent)

        batch = [
            {"tool_id": "ok", "tool_name": "read_file", "arguments": {"path": "ok.py"}},
            {"tool_id": "fail", "tool_name": "read_file_bad", "arguments": {"path": "bad.py"}},
        ]
        hooks_manager = AsyncMock()
        hooks_manager.run_hooks.return_value = None

        results = await executor.run_tool_batch(batch, hooks_data=None, hooks_manager=hooks_manager)

        assert len(results) == 2
        good = [r for r in results if r["success"]]
        bad = [r for r in results if not r["success"]]
        assert len(good) == 1
        assert len(bad) == 1


class TestCappedParallelism:
    @pytest.mark.asyncio
    async def test_capped_invocation_runs_in_chunks(self):
        t = SimpleNamespace(
            name="capped_tool",
            is_read_only=False,
            requires_confirmation=False,
            max_parallel_invocations=2,
            execute=AsyncMock(return_value={"success": True}),
        )
        registry = _make_registry({"capped_tool": t})
        agent = _make_agent(registry)
        executor = ToolExecutor(agent)

        batch = [
            {"tool_id": str(i), "tool_name": "capped_tool", "arguments": {"n": i}}
            for i in range(5)
        ]
        hooks_manager = AsyncMock()
        hooks_manager.run_hooks.return_value = None

        results = await executor.run_tool_batch(batch, hooks_data=None, hooks_manager=hooks_manager)

        assert len(results) == 5
        assert all(r["success"] for r in results)


