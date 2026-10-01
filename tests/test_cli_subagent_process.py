"""Exercise the production async CLI process path without external providers."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

from coderai.subagents.backends.base import CliSubagentDriver


async def test_injected_process_factory_preserves_launch_and_environment(tmp_path, monkeypatch):
    launches = []
    monkeypatch.setenv("CODERAI_PROCESS_TEST", "parent")

    async def communicate():
        return b"  text\xff\n", b"warning\n"

    async def spawn(*args, **kwargs):
        launches.append((args, kwargs))
        return SimpleNamespace(returncode=0, communicate=communicate)

    driver = CliSubagentDriver("fake", "fake", process_factory=spawn)
    result = await driver._run_command(
        ["fake", "argument"], str(tmp_path), env={"CODERAI_PROCESS_TEST": "child"}
    )
    assert result["stdout"] == "text\ufffd" and result["stderr"] == "warning"
    args, options = launches[0]
    assert args == ("fake", "argument") and options["cwd"] == str(tmp_path)
    assert options["start_new_session"] is True
    assert options["stdout"] == options["stderr"] == asyncio.subprocess.PIPE
    assert options["env"]["CODERAI_PROCESS_TEST"] == "child"


@pytest.mark.parametrize(
    ("program", "summary", "stdout", "stderr", "returncode"),
    [
        ('print(\'{"result":"done"}\')', "done", '{"result":"done"}', "", 0),
        ('print(\'{"summary":"done"}\')', "done", '{"summary":"done"}', "", 0),
        ('print("not JSON")', "not JSON", "not JSON", "", 0),
        (
            'import sys; print("out"); print("err", file=sys.stderr); sys.exit(3)',
            None,
            "out",
            "err",
            3,
        ),
    ],
)
async def test_real_process_results(tmp_path, program, summary, stdout, stderr, returncode):
    driver = CliSubagentDriver(sys.executable, sys.executable, timeout_seconds=5)
    result = await driver._run_command(
        [sys.executable, "-c", program], str(tmp_path), backend_name="test"
    )
    assert result["ok"] is (returncode == 0)
    assert result["returncode"] == returncode
    assert result["stdout"] == stdout and result["stderr"] == stderr
    if summary is not None:
        assert result["summary"] == summary
    else:
        assert result["error"] == "test exited with code 3: err"


async def test_spawn_failure_is_structured(tmp_path):
    driver = CliSubagentDriver("missing", "missing")
    result = await driver._run_command(
        [str(tmp_path / "missing-executable")], str(tmp_path), backend_name="test"
    )
    assert not result["ok"] and result["returncode"] is None
    assert result["error"].startswith("Failed to spawn test: ")


@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_and_cancellation_reap_the_process(tmp_path, monkeypatch, cancel):
    spawned = asyncio.Event()
    processes = []
    original = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        spawned.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    driver = CliSubagentDriver(
        sys.executable, sys.executable, timeout_seconds=5 if cancel else 0.05
    )
    task = asyncio.create_task(
        driver._run_command(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            str(tmp_path),
            backend_name="test",
        )
    )
    try:
        await asyncio.wait_for(spawned.wait(), 5)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await asyncio.wait_for(task, 5)
            assert result["error"] == "test timed out after 0.05s"
            assert result["returncode"] is None
        assert processes[0].returncode is not None
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.wait()
