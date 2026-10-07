"""Background shell-task worker (adapted).

CoderAI's worker is an out-of-process entry driven by a file-backed
``BackgroundTaskStore`` (spec/runtime/control JSON). CoderAI's jobs are
in-process (:class:`~coderai.background.store.JobStore`) with kill-tree
termination, so this worker supervises one shell command and reports
terminal state through ``JobStore.complete()``:

- spawn via asyncio subprocess, stdout/stderr appended to ``output_path``
- heartbeat callback every ``heartbeat_interval_s``
- ``is_cancelled()`` polled every ``control_poll_interval_s`` →
  TERM kill-tree, then KILL after ``kill_grace_period_s`` (same escalation
  as CoderAI's control loop)
- ``timeout_s`` → same TERM → KILL escalation, terminal ``failed`` with a
  timeout detail
- cancellation marks the job ``killed`` (via ``JobStore.kill`` +
  ``complete``); ``returncode == 0`` completes, else ``failed``
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from coderai.utils.subprocess_env import build_shell_env, kill_process_tree
from coderai.utils.bounded_process import terminate_owned_process

logger = logging.getLogger(__name__)

_SIGTERM = getattr(signal, "SIGTERM", signal.SIGTERM)


async def run_background_task_worker(
    *,
    job_id: str,
    session_id: str,
    command: str,
    shell_path: str,
    cwd: str,
    output_path: str | Path,
    timeout_s: float | None,
    heartbeat_interval_s: float = 5.0,
    control_poll_interval_s: float = 0.5,
    kill_grace_period_s: float = 2.0,
    is_cancelled: Callable[[], bool] | None = None,
    on_heartbeat: Callable[[], None] | None = None,
) -> Any:
    """Run one background shell command to terminal state.

    Returns the completed :class:`~coderai.background.models.Job`
    (or ``None`` when the job id is unknown to the store).
    """
    from coderai.background.manager import get_job_store

    store = get_job_store()
    output_file_path = Path(output_path)

    process: asyncio.subprocess.Process | None = None
    control_task: asyncio.Task[None] | None = None
    heartbeat_task: asyncio.Task[None] | None = None
    stop_event = asyncio.Event()
    cancel_requested = False
    timed_out = False

    async def _heartbeat_loop() -> None:
        while not stop_event.is_set():
            await asyncio.sleep(heartbeat_interval_s)
            if stop_event.is_set():
                return
            try:
                if on_heartbeat is not None:
                    on_heartbeat()
            except Exception:
                logger.debug("Background worker heartbeat callback failed", exc_info=True)

    def _terminate_process(force: bool = False) -> None:
        if process is None:
            return
        try:
            if force:
                terminate_owned_process(process.pid)
            elif process.returncode is None:
                kill_process_tree(process.pid, _SIGTERM)
        except Exception:
            logger.debug("Background worker terminate failed", exc_info=True)

    async def _control_loop() -> None:
        nonlocal cancel_requested
        kill_sent_at: float | None = None
        while not stop_event.is_set():
            await asyncio.sleep(control_poll_interval_s)
            try:
                job = store.get(job_id, session_id)
                cancelled = bool(is_cancelled and is_cancelled()) or (
                    job is not None and job.status in ("stopping", "killed")
                )
            except Exception:
                cancelled = False
            if not cancelled:
                continue
            cancel_requested = True
            _terminate_process(force=False)
            kill_sent_at = kill_sent_at or time.monotonic()
            if (
                process is not None
                and process.returncode is None
                and time.monotonic() - kill_sent_at >= kill_grace_period_s
            ):
                _terminate_process(force=True)

    try:
        output_file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_file_path, "ab") as output_file:
            # Mirror the foreground Shell tool's environment (which also sets
            # $SHELL to the shell actually running the command).
            env = build_shell_env(shell_path)
            spawn_kwargs: dict[str, Any] = {
                "stdin": subprocess.DEVNULL,
                "stdout": output_file,
                "stderr": output_file,
                "cwd": cwd,
                "env": env,
            }
            if os.name == "nt":
                spawn_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            else:
                spawn_kwargs["start_new_session"] = True

            process = await asyncio.create_subprocess_exec(
                shell_path, "-c", command, **spawn_kwargs
            )

            heartbeat_task = asyncio.create_task(_heartbeat_loop())
            control_task = asyncio.create_task(_control_loop())
            if timeout_s is None:
                returncode = await process.wait()
            else:
                try:
                    returncode = await asyncio.wait_for(process.wait(), timeout=timeout_s)
                except TimeoutError:
                    timed_out = True
                    _terminate_process(force=False)
                    try:
                        returncode = await asyncio.wait_for(
                            process.wait(), timeout=kill_grace_period_s
                        )
                    except TimeoutError:
                        _terminate_process(force=True)
                        returncode = await process.wait()
    except asyncio.CancelledError:
        cancel_requested = True
        _terminate_process(force=False)
        if process is not None:
            try:
                await asyncio.wait_for(process.wait(), timeout=kill_grace_period_s)
            except TimeoutError:
                _terminate_process(force=True)
                await process.wait()
        store.kill(job_id, session_id, reason="Worker cancelled")
        store.complete(
            job_id,
            ok=False,
            exit_code=process.returncode if process is not None else None,
            detail="Worker cancelled",
        )
        raise
    except Exception as exc:
        _terminate_process(force=True)
        if process is not None:
            await process.wait()
        logger.exception("Background task worker failed")
        return store.complete(job_id, ok=False, detail=str(exc))
    finally:
        if cancel_requested or timed_out:
            # The shell may exit on TERM while descendants ignore it.
            _terminate_process(force=True)
        stop_event.set()
        for task in (heartbeat_task, control_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    if timed_out:
        return store.complete(
            job_id, ok=False, exit_code=returncode, detail=f"Command timed out after {timeout_s:g}s"
        )
    if cancel_requested:
        store.kill(job_id, session_id, reason="Killed")
        return store.complete(job_id, ok=False, exit_code=returncode, detail="Killed")
    if returncode == 0:
        return store.complete(job_id, ok=True, exit_code=0)
    return store.complete(
        job_id,
        ok=False,
        exit_code=returncode,
        detail=f"Command failed with exit code {returncode}",
    )
