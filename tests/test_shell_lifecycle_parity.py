"""Foreground/background handoff preserves process ownership and cancellation."""

from __future__ import annotations

import asyncio
import os
import pathlib
import shlex
import sys
from types import SimpleNamespace

import pytest

from coderai.background import get_job_store, reset_job_store
from coderai.tools.legacy.types import ToolExecutionContext
from coderai.tools.shell import handle_bash_tool


@pytest.fixture
def shell_context(tmp_path, monkeypatch):
    import coderai.tools.shell as shell

    reset_job_store()
    monkeypatch.setattr(shell, "BACKGROUND_OUTPUT_DIR", tmp_path / "logs")
    context = ToolExecutionContext(
        session_id=tmp_path.name, project_root=str(tmp_path), sandbox_mode="danger-full-access"
    )
    yield context
    get_job_store().kill_all(context.session_id)
    reset_job_store()


async def wait_settled(job):
    async with asyncio.timeout(5):
        while job.status in ("running", "stopping"):
            await asyncio.sleep(0.02)


async def test_timeout_detaches_same_process_and_preserves_output(shell_context):
    starts = []
    shell_context.on_process_start = lambda pid, command: starts.append(pid)
    script = "import time; print('before', flush=True); time.sleep(1.3); print('after')"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    result = await asyncio.to_thread(
        handle_bash_tool, {"command": command, "timeout": 1}, shell_context
    )
    assert result.ok and result.metadata["timeoutDetached"]
    job = get_job_store().get(result.metadata["backgroundTaskId"], shell_context.session_id)
    assert job.process_id == starts[0] and job.status == "running"
    os.kill(job.process_id, 0)
    await wait_settled(job)
    assert job.status == "completed"
    assert pathlib.Path(job.output_path).read_text().strip() == "before\nafter"


async def test_fast_foreground_task_returns_result_without_completion_notice(shell_context):
    notices = []
    shell_context.on_background_process_complete = notices.append
    result = await asyncio.to_thread(
        handle_bash_tool, {"command": "printf quick", "timeout": 1}, shell_context
    )
    assert result.ok and "quick" in result.output
    assert not result.metadata["runInBackground"] and not notices
    assert get_job_store().get(result.metadata["taskId"]).status == "completed"


async def test_cancelled_handoff_reaps_foreground_process(shell_context):
    started = asyncio.Event()
    loop = asyncio.get_running_loop()
    pids = []

    def on_start(pid, command):
        pids.append(pid)
        loop.call_soon_threadsafe(started.set)

    shell_context.on_process_start = on_start
    task = asyncio.create_task(
        asyncio.to_thread(handle_bash_tool, {"command": "sleep 30"}, shell_context)
    )
    await asyncio.wait_for(started.wait(), 2)
    shell_context.cancellation_event.set()
    result = await asyncio.wait_for(task, 3)
    assert not result.ok
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)


def test_config_disabled_handoff_uses_hard_timeout(shell_context, monkeypatch):
    import coderai.tools.shell as shell

    captured = []
    shell_context.session_manager = SimpleNamespace(
        get_resolved_settings=lambda: {"bashAutoBackgroundOnTimeout": False}
    )

    def execute(*args, **kwargs):
        captured.append(kwargs["timeout_ms_override"])
        return {
            "stdout": "",
            "stderr": "",
            "timed_out": True,
            "timeout_ms": 1000,
            "exit_code": None,
            "signal": "SIGTERM",
            "duration_ms": 1000,
        }

    monkeypatch.setattr(shell, "_execute_shell_command", execute)
    result = handle_bash_tool({"command": "sleep 30", "timeout": 1}, shell_context)
    assert captured == [1000] and not result.ok
    assert not result.metadata.get("timeoutDetached")


def test_readonly_mask_cannot_launch_background_jobs(shell_context):
    shell_context.allowed_tools = ["bash", "read"]
    result = handle_bash_tool(
        {"command": "sleep 30", "run_in_background": True, "description": "test"}, shell_context
    )
    assert not result.ok and "disabled" in result.error


@pytest.mark.parametrize("timeout", [True, 0, -1, 301, 1.2])
def test_invalid_foreground_timeout_is_rejected(shell_context, timeout):
    result = handle_bash_tool({"command": "printf invalid", "timeout": timeout}, shell_context)
    assert not result.ok and "positive integer" in result.error
