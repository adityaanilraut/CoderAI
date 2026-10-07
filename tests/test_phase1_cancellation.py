"""Path locks outlive cancelled callers while their synchronous workers run."""

from __future__ import annotations

import asyncio
import gc
import os
import sys
import threading
from types import SimpleNamespace

import pytest

from coderai.tools.legacy.executor import ToolExecutor
from coderai.tools.legacy.path_lock import PathLockManager
from coderai.tools.legacy.types import ToolExecutionContext


@pytest.mark.asyncio
async def test_cancelled_write_waiter_does_not_poison_path_lock(tmp_path):
    locks = PathLockManager()
    path = str(tmp_path / "shared.txt")
    entered = asyncio.Event()

    async def write():
        async with locks.acquire_write_lock(path):
            entered.set()

    async with locks.acquire_write_lock(path):
        waiting = asyncio.create_task(write())
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert locks._locks[path].locked()
        assert not entered.is_set()

    assert not locks._locks[path].locked()
    await asyncio.wait_for(write(), 1)
    assert entered.is_set()
    assert not locks._locks[path].locked()


@pytest.mark.asyncio
async def test_async_handler_timeout_cancels_worker_before_releasing_lock(tmp_path, monkeypatch):
    locks = PathLockManager()
    monkeypatch.setattr("coderai.tools.legacy.path_lock.get_path_lock_manager", lambda: locks)
    executor = ToolExecutor(project_root=str(tmp_path))
    context = ToolExecutionContext(session_id="timeout", project_root=str(tmp_path))
    stopped = asyncio.Event()

    async def handler(_args, _context):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    definition = SimpleNamespace(name="write", handler=handler, timeout_ms=20)
    result = await executor._run_handler(definition, {"file_path": "shared.txt"}, context, None)
    assert not result.ok and "TOOL_TIMEOUT" in result.error
    await asyncio.wait_for(stopped.wait(), 1)
    async with locks.acquire_write_lock("shared.txt", str(tmp_path)):
        assert stopped.is_set()


@pytest.mark.asyncio
async def test_cancelled_foreground_shell_reaps_process(tmp_path):
    from coderai.tools.shell import _execute_shell_command

    executor = ToolExecutor(project_root=str(tmp_path))
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    exited = asyncio.Event()
    pids = []

    def on_start(pid, _command):
        pids.append(pid)
        loop.call_soon_threadsafe(started.set)

    context = ToolExecutionContext(
        session_id="cancel-shell",
        project_root=str(tmp_path),
        sandbox_mode="danger-full-access",
        on_process_start=on_start,
        on_process_exit=lambda _pid: loop.call_soon_threadsafe(exited.set),
    )

    def handler(_args, ctx):
        return _execute_shell_command(
            "/bin/sh", ["-c", "exec sleep 30"], str(tmp_path), "exec sleep 30", ctx
        )

    definition = SimpleNamespace(name="bash", handler=handler, timeout_ms=None)
    running = asyncio.create_task(executor._run_handler(definition, {}, context, None))
    try:
        await asyncio.wait_for(started.wait(), 2)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        await asyncio.wait_for(exited.wait(), 2)
        with pytest.raises(ProcessLookupError):
            os.kill(pids[0], 0)
    finally:
        context.cancellation_event.set()
        await asyncio.gather(*getattr(executor, "_handler_cleanup_tasks", ()))


@pytest.mark.asyncio
async def test_shell_timeout_control_remains_readable_after_completion(tmp_path):
    from coderai.tools.shell import _execute_shell_command

    controls = []
    context = ToolExecutionContext(
        session_id="shell-control",
        project_root=str(tmp_path),
        sandbox_mode="danger-full-access",
        on_process_timeout_control=lambda _pid, control: controls.append(control),
    )
    result = await asyncio.to_thread(
        _execute_shell_command, "/bin/sh", ["-c", "true"], str(tmp_path), "true", context
    )
    assert result["exit_code"] == 0
    control = controls[0]
    finished = threading.Event()
    infos = []

    def read_settled_control():
        infos.append(control.set_timeout_ms(1000))
        finished.set()

    threading.Thread(target=read_settled_control, daemon=True).start()
    assert await asyncio.to_thread(finished.wait, 1), "settled timeout control deadlocked"
    assert infos[0].timeout_ms == result["timeout_ms"]


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_ms", [None, 150], ids=["cancel", "tool-timeout"])
async def test_powershell_foreground_cancellation_reaps_worker_before_releasing_resources(
    tmp_path, monkeypatch, timeout_ms
):
    import coderai.tools.shell as shell
    from coderai.tools.legacy.resources import ResourceAccess, get_resource_scheduler

    pid_file = tmp_path / "powershell.pid"
    script = (
        "import os, pathlib, time; "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(30)"
    )
    monkeypatch.setattr(shell, "_resolve_pwsh_executable", lambda: sys.executable)
    monkeypatch.setattr(
        shell, "wrap_sandbox_command", lambda *_a, **_kw: ([sys.executable, "-c", script], {})
    )
    context = ToolExecutionContext(
        session_id="pwsh-cancel", project_root=str(tmp_path), sandbox_mode="danger-full-access"
    )
    executor = ToolExecutor(str(tmp_path))
    definition = SimpleNamespace(name="pwsh", handler=shell.handle_pwsh_tool, timeout_ms=timeout_ms)
    task = asyncio.create_task(
        executor._run_handler(definition, {"command": "probe"}, context, None)
    )
    try:
        async with asyncio.timeout(2):
            while not pid_file.exists():
                await asyncio.sleep(0.01)
        pid = int(pid_file.read_text())
        if timeout_ms is None:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await asyncio.wait_for(task, 2)
            assert not result.ok and "TOOL_TIMEOUT" in result.error
        async with get_resource_scheduler().acquire((ResourceAccess(kind="all"),)):
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
    finally:
        context.cancellation_event.set()
        await asyncio.gather(*getattr(executor, "_handler_cleanup_tasks", ()))


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
        assert entry.locked(), "caller cancellation released a live worker's lock"
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
        assert entry.locked()
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
