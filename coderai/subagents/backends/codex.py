"""OpenAI Codex CLI Subagent Driver.

Drives OpenAI Codex CLI as an external subagent backend.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from typing import Any

from coderai.subagents.backends.base import CliSubagentDriver

logger = logging.getLogger(__name__)


@dataclass
class CodexConfig:
    approval_policy: str = "never"  # "never" | "approve-for-me" | "dangerously-bypass"
    timeout_seconds: float = 180.0
    cwd: str = "."
    codex_bin: str | None = None
    extra_args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None


class CodexDriver(CliSubagentDriver):
    """Spawns and executes tasks using OpenAI Codex CLI."""

    def __init__(self, config: CodexConfig | None = None) -> None:
        self.config = config or CodexConfig()
        super().__init__(
            bin_name=self.config.codex_bin or "codex",
            default_bin="codex",
            timeout_seconds=self.config.timeout_seconds,
        )

    async def execute(self, prompt: str, project_root: str | None = None) -> dict[str, Any]:
        """Execute a subagent prompt via Codex CLI."""
        bin_name = self.config.codex_bin or shutil.which("codex") or "codex"
        cwd = project_root or self.config.cwd

        if self.config.approval_policy not in {"never", "approve-for-me", "dangerously-bypass"}:
            raise ValueError("Invalid Codex approval policy")
        cmd = [bin_name, "exec", "--json", "-c", 'approval_policy="never"']
        if self.config.approval_policy == "dangerously-bypass":
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        elif self.config.approval_policy == "approve-for-me":
            cmd.append("--approve-for-me")
        else:
            cmd.extend(["--sandbox", "workspace-write"])

        cmd.extend(self.config.extra_args)

        cmd.append("-")
        res = await self._run_command(
            cmd,
            cwd=cwd,
            env=self.config.env,
            backend_name="Codex",
            input_data=prompt.encode("utf-8"),
        )
        return {
            "ok": res["ok"],
            "status": "completed"
            if res["ok"]
            else ("timeout" if "timed out" in str(res.get("error", "")) else "failed"),
            "summary": res.get("summary") or res.get("error") or "Codex task finished.",
            "duration_seconds": res.get("elapsedSeconds", 0.0),
            "stdout": res.get("stdout", ""),
            "stderr": res.get("stderr", ""),
            "error": res.get("error"),
        }
