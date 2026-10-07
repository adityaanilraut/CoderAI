"""Bound subprocess collection while preserving process-tree cleanup."""

from __future__ import annotations

import asyncio
import os
import subprocess
import signal
import threading
import time
from typing import Any

from coderai.utils.subprocess_env import kill_process_tree

STDOUT_LIMIT = 20_000_000
STDERR_LIMIT = 64 * 1024


class OutputLimitError(RuntimeError):
    pass


def terminate_owned_process(pid: int) -> None:
    """Only for children started with start_new_session=True."""
    if os.name != "nt":
        try:
            os.killpg(pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
    kill_process_tree(pid)


def bounded_run(
    argv: list[str],
    *,
    timeout: float,
    stdout_limit: int = STDOUT_LIMIT,
    cancellation_event: threading.Event | None = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess[bytes]:
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name != "nt",
        **kwargs,
    )
    output = [bytearray(), bytearray()]
    overflow = threading.Event()
    failures: list[Exception] = []

    def collect(index: int) -> None:
        stream = proc.stdout if index == 0 else proc.stderr
        assert stream is not None
        try:
            while chunk := stream.read(65536):
                if index == 0:
                    remaining = max(0, stdout_limit - len(output[0]))
                    output[0].extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        overflow.set()
                        terminate_owned_process(proc.pid)
                        break
                else:
                    output[1].extend(chunk)
                    del output[1][:-STDERR_LIMIT]
        except Exception as exc:
            failures.append(exc)
        finally:
            stream.close()

    threads = [threading.Thread(target=collect, args=(i,), daemon=True) for i in range(2)]
    deadline = time.monotonic() + timeout
    for thread in threads:
        thread.start()
    try:
        while proc.poll() is None:
            if isinstance(cancellation_event, threading.Event) and cancellation_event.is_set():
                raise subprocess.TimeoutExpired(argv, timeout)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            try:
                proc.wait(timeout=min(0.05, remaining))
            except subprocess.TimeoutExpired:
                continue
        for thread in threads:
            while thread.is_alive() and time.monotonic() < deadline:
                if isinstance(cancellation_event, threading.Event) and cancellation_event.is_set():
                    raise subprocess.TimeoutExpired(argv, timeout)
                thread.join(timeout=0.05)
        if any(thread.is_alive() for thread in threads):
            raise subprocess.TimeoutExpired(argv, timeout)
        if overflow.is_set():
            raise OutputLimitError(
                f"Output exceeded {stdout_limit} bytes; narrow the search and retry"
            )
        if failures:
            raise failures[0]
        return subprocess.CompletedProcess(
            argv, proc.returncode, bytes(output[0]), bytes(output[1])
        )
    finally:
        # Also kill descendants holding pipes after their parent has exited.
        terminate_owned_process(proc.pid)
        proc.wait()
        for thread in threads:
            thread.join(timeout=2)


async def bounded_communicate(
    proc: Any, data: bytes, *, stdout_limit: int = STDOUT_LIMIT
) -> tuple[bytes, bytes]:
    async def collect(stream: Any, limit: int, truncate: bool = False) -> bytes:
        result = bytearray()
        while chunk := await stream.read(65536):
            if not truncate and len(result) + len(chunk) > limit:
                raise OutputLimitError(f"Output exceeded {limit} bytes")
            result.extend(chunk)
            if truncate:
                del result[:-limit]
        return bytes(result)

    async def send() -> None:
        if proc.stdin:
            try:
                proc.stdin.write(data)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                proc.stdin.close()

    tasks = [
        asyncio.create_task(send()),
        asyncio.create_task(collect(proc.stdout, stdout_limit)),
        asyncio.create_task(collect(proc.stderr, STDERR_LIMIT, True)),
    ]
    try:
        results = await asyncio.gather(*tasks)
        await proc.wait()
        assert isinstance(results[1], bytes) and isinstance(results[2], bytes)
        return results[1], results[2]
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
