"""Base CLI Subagent Driver providing shared subprocess execution and JSON parsing."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from typing import Any

from coderai.subagents.backends.process import ProcessFactory, RunningProcess, stop_process

logger = logging.getLogger(__name__)


class CliSubagentDriver:
    """Base driver for external CLI-based subagents."""

    def __init__(
        self,
        bin_name: str,
        default_bin: str,
        timeout_seconds: float = 180.0,
        *,
        process_factory: ProcessFactory | None = None,
    ) -> None:
        self.bin_name = bin_name or default_bin
        self.default_bin = default_bin
        self.timeout_seconds = timeout_seconds
        self._process_factory = process_factory

    def is_available(self) -> bool:
        """Check if CLI binary is available on PATH."""
        return shutil.which(self.bin_name) is not None or shutil.which(self.default_bin) is not None

    async def _run_command(
        self,
        cmd: list[str],
        cwd: str,
        env: dict[str, str] | None = None,
        backend_name: str = "cli_subagent",
        input_data: bytes | None = None,
    ) -> dict[str, Any]:
        """Execute the CLI subprocess and return a structured subagent response dict."""
        run_env = os.environ.copy()
        if env:
            run_env.update(env)

        start_time = time.time()
        proc: RunningProcess | None = None
        try:
            spawn = self._process_factory or asyncio.create_subprocess_exec
            proc = await spawn(
                *cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=run_env,
                start_new_session=True,
                stdin=asyncio.subprocess.PIPE if input_data is not None else None,
            )
            from coderai.utils.bounded_process import bounded_communicate

            collect = (
                bounded_communicate(proc, input_data or b"", stdout_limit=4_000_000)
                if hasattr(proc, "stdout") and hasattr(proc, "stderr")
                else proc.communicate(input=input_data)
                if input_data is not None
                else proc.communicate()
            )
            stdout_b, stderr_b = await asyncio.wait_for(collect, timeout=self.timeout_seconds)
            elapsed = time.time() - start_time
            stdout = stdout_b.decode("utf-8", errors="replace").strip()
            stderr = stderr_b.decode("utf-8", errors="replace").strip()

            if proc.returncode == 0:
                summary = stdout
                try:
                    parsed = json.loads(stdout)
                    if isinstance(parsed, dict):
                        summary = parsed.get("result") or parsed.get("summary") or stdout
                except Exception:
                    pass

                return {
                    "ok": True,
                    "backend": backend_name,
                    "summary": summary,
                    "stdout": stdout,
                    "stderr": stderr,
                    "elapsedSeconds": elapsed,
                    "returncode": proc.returncode,
                }

            return {
                "ok": False,
                "backend": backend_name,
                "error": f"{backend_name} exited with code {proc.returncode}: {stderr or stdout}",
                "stdout": stdout,
                "stderr": stderr,
                "elapsedSeconds": elapsed,
                "returncode": proc.returncode,
            }

        except asyncio.TimeoutError:
            if proc:
                await stop_process(proc)
            return {
                "ok": False,
                "backend": backend_name,
                "error": f"{backend_name} timed out after {self.timeout_seconds}s",
                "elapsedSeconds": time.time() - start_time,
                "returncode": None,
            }
        except asyncio.CancelledError:
            if proc:
                await stop_process(proc)
            raise
        except Exception as e:
            if proc is not None and proc.returncode is None:
                await stop_process(proc)
            return {
                "ok": False,
                "backend": backend_name,
                "error": f"Failed to spawn {backend_name}: {e}",
                "elapsedSeconds": time.time() - start_time,
                "returncode": None,
            }
