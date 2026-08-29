"""ACP (Agent Client Protocol) Subagent Driver for out-of-process subagent execution."""

from __future__ import annotations

import logging
from typing import Any

from coderai.core.subagent_backends.base import CliSubagentDriver
from coderai.core.acp.runner import AcpSubagentRunner, AcpRunConfig

logger = logging.getLogger(__name__)


class AcpSubagentDriver(CliSubagentDriver):
    """Driver for out-of-process subagents speaking the Agent Client Protocol (ACP)."""

    def __init__(
        self,
        bin_name: str = "acp-agent",
        default_bin: str = "acp-agent",
        timeout_seconds: float = 180.0,
    ) -> None:
        super().__init__(bin_name, default_bin, timeout_seconds)

    async def execute(
        self,
        prompt: str,
        cwd: str,
        env: dict[str, str] | None = None,
        extra_args: list[str] | None = None,
    ) -> dict[str, Any]:
        """Execute an ACP subagent subprocess using the ACP protocol runner."""
        runner = AcpSubagentRunner(
            AcpRunConfig(
                command=self.bin_name,
                cwd=cwd,
                timeout_seconds=self.timeout_seconds,
            )
        )
        res = await runner.execute(prompt)
        return {
            "ok": res.get("ok", False),
            "backend": "acp",
            "summary": res.get("summary", ""),
            "status": res.get("status", "completed" if res.get("ok") else "failed"),
            "error": res.get("error"),
            "duration_seconds": res.get("duration_seconds", 0.0),
        }
