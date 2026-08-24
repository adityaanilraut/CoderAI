"""Claude Code CLI Subagent Driver.

Drives Anthropic's Claude Code CLI as an external subagent backend.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from typing import Any

from coderai.core.subagent_backends.base import CliSubagentDriver

logger = logging.getLogger(__name__)


@dataclass
class ClaudeCodeConfig:
    permission_mode: str = "dontAsk"  # "dontAsk" | "acceptEdits" | "bypassPermissions"
    timeout_seconds: float = 180.0
    cwd: str = "."
    claude_bin: str | None = None
    extra_args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None


class ClaudeCodeDriver(CliSubagentDriver):
    """Spawns and executes tasks using Anthropic's Claude Code CLI."""

    def __init__(self, config: ClaudeCodeConfig | None = None) -> None:
        self.config = config or ClaudeCodeConfig()
        super().__init__(
            bin_name=self.config.claude_bin or "claude",
            default_bin="claude",
            timeout_seconds=self.config.timeout_seconds,
        )

    async def execute(self, prompt: str, project_root: str | None = None) -> dict[str, Any]:
        """Execute a subagent prompt via Claude Code CLI."""
        bin_name = self.config.claude_bin or shutil.which("claude") or "claude"
        cwd = project_root or self.config.cwd

        cmd = [bin_name, "-p", prompt, "--output-format", "json"]

        if self.config.permission_mode in ("bypassPermissions", "dangerously-bypass"):
            cmd.append("--dangerously-skip-permissions")
        elif self.config.permission_mode == "acceptEdits":
            cmd.extend(["--permission-mode", "acceptEdits"])
        else:
            cmd.extend(["--permission-mode", "dontAsk"])

        cmd.extend(self.config.extra_args)

        res = await self._run_command(cmd, cwd=cwd, env=self.config.env, backend_name="Claude Code")
        return {
            "ok": res["ok"],
            "status": "completed"
            if res["ok"]
            else ("timeout" if "timed out" in str(res.get("error", "")) else "failed"),
            "summary": res.get("summary") or res.get("error") or "Claude Code task finished.",
            "duration_seconds": res.get("elapsedSeconds", 0.0),
            "stdout": res.get("stdout", ""),
            "stderr": res.get("stderr", ""),
            "error": res.get("error"),
        }
