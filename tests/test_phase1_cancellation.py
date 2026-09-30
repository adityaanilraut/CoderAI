"""Path locks outlive cancelled callers while their synchronous workers run."""

from __future__ import annotations

import asyncio
import gc
import threading
from types import SimpleNamespace

import pytest

from coderai.tools.legacy.executor import ToolExecutor
from coderai.tools.legacy.path_lock import PathLockManager
from coderai.tools.legacy.types import ToolExecutionContext


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_ms", [None, 60_000])
@pytest.mark.parametrize("late_error", [False, True])
async def test_cancelled_thread_retains_write_lock(tmp_path, monkeypatch, timeout_ms, late_error):
    locks = PathLockManager()
    monkeypatch.setattr("coderai.tools.legacy.path_lock.get_path_lock_manager", lambda: locks)
    executor = ToolExecutor(project_root=str(tmp_path))
    context = ToolExecutionContext(session_id="cancel", project_root=str(tmp_path))
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    second_entered = asyncio.Event()
    unexpected_errors = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, event: unexpected_errors.append(event))

    def writer(_args, _context):
        loop.call_soon_threadsafe(started.set)
        if not release.wait(5):
            raise RuntimeError("test worker barrier was never released")
        if late_error:
            raise RuntimeError("late worker failure")
        return "first writer completed"

    async def second_writer(_args, _context):
        second_entered.set()
        return "second writer completed"

    first_def = SimpleNamespace(name="write", handler=writer, timeout_ms=timeout_ms)
    second_def = SimpleNamespace(name="write", handler=second_writer, timeout_ms=None)
    args = {"file_path": "shared.txt"}
    first = asyncio.create_task(executor._run_handler(first_def, args, context, None))
    second = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first, 1)

        entry = locks._locks[str(tmp_path / "shared.txt")]
        assert entry._write_lock.locked(), "caller cancellation released a live worker's lock"
        second = asyncio.create_task(executor._run_handler(second_def, args, context, None))
        await asyncio.sleep(0)
        assert not second_entered.is_set()
        assert not second.done()
    finally:
        release.set()
        if second is not None:
            result = await asyncio.wait_for(second, 2)
            assert result.ok
        # Drain retained cleanup before forcing collection of completed tasks.
        pending = getattr(executor, "_handler_cleanup_tasks", set())
        if pending:
            await asyncio.wait_for(asyncio.gather(*tuple(pending)), 2)
        await asyncio.sleep(0)
        gc.collect()
        loop.set_exception_handler(previous_handler)
    assert not unexpected_errors


@pytest.mark.asyncio
async def test_timeout_observes_late_worker_failure_and_releases_lock(tmp_path, monkeypatch):
    locks = PathLockManager()
    monkeypatch.setattr("coderai.tools.legacy.path_lock.get_path_lock_manager", lambda: locks)
    executor = ToolExecutor(project_root=str(tmp_path))
    context = ToolExecutionContext(session_id="timeout", project_root=str(tmp_path))
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    unexpected_errors = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, event: unexpected_errors.append(event))

    def writer(_args, _context):
        loop.call_soon_threadsafe(started.set)
        if not release.wait(5):
            raise RuntimeError("test worker barrier was never released")
        raise RuntimeError("late worker failure")

    definition = SimpleNamespace(name="write", handler=writer, timeout_ms=30)
    call = asyncio.create_task(
        executor._run_handler(definition, {"file_path": "shared.txt"}, context, None)
    )
    try:
        await asyncio.wait_for(started.wait(), 2)
        result = await asyncio.wait_for(call, 1)
        assert not result.ok and "TOOL_TIMEOUT" in result.error
        entry = locks._locks[str(tmp_path / "shared.txt")]
        assert entry._write_lock.locked()
    finally:
        release.set()
        pending = getattr(executor, "_handler_cleanup_tasks", set())
        if pending:
            await asyncio.wait_for(asyncio.gather(*tuple(pending)), 2)
        # Acquiring the same lock proves the worker and its cleanup finished.
        async with asyncio.timeout(2):
            async with locks.acquire_write_lock("shared.txt", str(tmp_path)):
                pass
        await asyncio.sleep(0)
        gc.collect()
        loop.set_exception_handler(previous_handler)
    assert not unexpected_errors


@pytest.mark.asyncio
async def test_async_handler_cancellation_stops_handler_and_releases_lock(tmp_path, monkeypatch):
    locks = PathLockManager()
    monkeypatch.setattr("coderai.tools.legacy.path_lock.get_path_lock_manager", lambda: locks)
    executor = ToolExecutor(project_root=str(tmp_path))
    context = ToolExecutionContext(session_id="async", project_root=str(tmp_path))
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def writer(_args, _context):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    definition = SimpleNamespace(name="write", handler=writer, timeout_ms=None)
    call = asyncio.create_task(
        executor._run_handler(definition, {"file_path": "shared.txt"}, context, None)
    )
    await asyncio.wait_for(started.wait(), 2)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(call, 1)
    await asyncio.wait_for(stopped.wait(), 1)
    async with asyncio.timeout(2):
        async with locks.acquire_write_lock("shared.txt", str(tmp_path)):
            pass
