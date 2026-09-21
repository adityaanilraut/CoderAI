from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pathlib import Path

ToolPolicyMode = Literal["inherit", "allowlist"]


@dataclass(frozen=True, slots=True, kw_only=True)
class SubagentTypeDefinition:
    name: str
    description: str
    when_to_use: str = ""
    allowed_tools: tuple[str, ...] | None = None
    exclude_tools: tuple[str, ...] = ()
    supports_background: bool = True
    system_prompt: str = ""
    mode: str = "general"
    source: str = "builtin"
    source_path: Path | None = None

    @property
    def tools(self) -> tuple[str, ...] | None:
        return self.allowed_tools


BUILTIN_SUBAGENT_TYPES: tuple[str, ...] = ("coder", "explore", "plan")


@dataclass(slots=True)
class SubagentRunRecord:
    """Lightweight durable record for one launch (mirrors store rows)."""

    agent_id: str
    subagent_type: str
    status: str = "idle"
    description: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    last_task_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
