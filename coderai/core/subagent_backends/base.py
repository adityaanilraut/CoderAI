"""Base CLI Subagent Driver providing shared subprocess execution and JSON parsing."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from typing import Any

logger = logging.getLogger(__name__)


class CliSubagentDriver:
    """Base driver for external CLI-based subagents."""

    def __init__(self, bin_name: str, default_bin: str, timeout_seconds: float = 180.0) -> None:
        self.bin_name = bin_name or default_bin
        self.default_bin = default_bin
        self.timeout_seconds = timeout_seconds

    def is_available(self) -> bool:
        """Check if CLI binary is available on PATH."""
        return shutil.which(self.bin_name) is not None or shutil.which(self.default_bin) is not None

    async def _run_command(
        self,
        cmd: list[str],
        cwd: str,
        env: dict[str, str] | None = None,
        backend_name: str = "cli_subagent",
    ) -> dict[str, Any]:
        """Execute the CLI subprocess and return a structured subagent response dict."""
        run_env = os.environ.copy()
        if env:
            run_env.update(env)

        start_time = time.time()
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                env=run_env,
            )
            elapsed = time.time() - start_time
            stdout = proc.stdout.strip()
            stderr = proc.stderr.strip()

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

        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "backend": backend_name,
                "error": f"{backend_name} timed out after {self.timeout_seconds}s",
                "elapsedSeconds": time.time() - start_time,
                "returncode": None,
            }
        except Exception as e:
            return {
                "ok": False,
                "backend": backend_name,
                "error": f"Failed to spawn {backend_name}: {e}",
                "elapsedSeconds": time.time() - start_time,
                "returncode": None,
            }
