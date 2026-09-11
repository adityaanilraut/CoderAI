# Ported from coderai/core/subagent.py - kimi structure (subagents/output.py).
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from coderai.orchestration import status_to_stop_reason

@dataclass
class SubAgentResult:
    """Aggregated output from a sub-agent execution."""

    task_id: str
    session_id: str
    status: str  # "completed" | "failed" | "interrupted" | "timeout" | "max_iterations" | "budget_exceeded"
    summary: str
    active_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    iterations: int = 0
    tool_calls_count: int = 0
    duration_seconds: float = 0.0
    error: str | None = None
    artifacts: list[str] = field(default_factory=list)
    diffs: list[dict[str, Any]] = field(default_factory=list)
    exit_code: int = 0
    token_telemetry: dict[str, Any] = field(default_factory=dict)
    parent_agent_id: str | None = None
    root_agent_id: str | None = None
    depth: int = 0
    children_ids: list[str] = field(default_factory=list)
    lifecycle_events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def stop_reason(self) -> str:
        """Harness stop-reason vocabulary derived from the internal status.

        Mirrors ``SubagentStopReason``: ``completed | aborted | error |
        max-tokens | refusal``.
        """
        return status_to_stop_reason(self.status)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "status": self.status,
            "stopReason": self.stop_reason,
            "summary": self.summary,
            "active_tokens": self.active_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
            "iterations": self.iterations,
            "tool_calls_count": self.tool_calls_count,
            "duration_seconds": self.duration_seconds,
            "error": self.error,
            "artifacts": self.artifacts,
            "diffs": self.diffs,
            "exit_code": self.exit_code,
            "token_telemetry": self.token_telemetry,
            "parent_agent_id": self.parent_agent_id,
            "root_agent_id": self.root_agent_id,
            "depth": self.depth,
            "children_ids": self.children_ids,
            "lifecycle_events": self.lifecycle_events,
        }

    def format_markdown(self) -> str:
        status_badge = "✅ COMPLETED" if self.status == "completed" else f"⚠️ {self.status.upper()}"
        lines = [
            f"### Sub-Agent Task Result [{self.task_id}] — {status_badge}",
            f"**Status**: `{self.status}` | **Exit Code**: `{self.exit_code}` | **Iterations**: `{self.iterations}` | **Tokens**: `{self.total_tokens}`",
        ]
        if self.error:
            lines.append(f"\n> ❌ **Error**: {self.error}\n")
        lines.append("\n**Findings & Summary**:")
        lines.append(self.summary.strip() or "No summary provided.")
        if self.artifacts:
            lines.append("\n**Artifacts/Files Examined**:")
            for art in self.artifacts:
                lines.append(f"- `{art}`")
        if self.diffs:
            lines.append(f"\n**Files Modified ({len(self.diffs)})**:")
            for d in self.diffs:
                fp = d.get("file_path", "unknown")
                lines.append(f"- `{fp}`")
        return "\n".join(lines)
