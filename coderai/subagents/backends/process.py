"""Process interface and shutdown shared by external CLI driver outcomes."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from collections.abc import Awaitable
from typing import Protocol


class RunningProcess(Protocol):
    @property
    def pid(self) -> int: ...

    @property
    def returncode(self) -> int | None: ...

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]: ...

    async def wait(self) -> int: ...

    def kill(self) -> None: ...


class ProcessFactory(Protocol):
    def __call__(
        self,
        *args: str,
        cwd: str,
        stdout: int,
        stderr: int,
        env: dict[str, str],
        start_new_session: bool,
        stdin: int | None = ...,
    ) -> Awaitable[RunningProcess]: ...


async def stop_process(process: RunningProcess) -> None:
    """Kill the process group, fall back to the child, then wait for reaping."""
    try:
        if sys.platform == "win32":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except Exception:
        pass
