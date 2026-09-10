# Ported from coderai/core/subagent_types.py - kimi structure (subagents/models.py).
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ToolPolicyMode = Literal["inherit", "allowlist"]

@dataclass(frozen=True, slots=True, kw_only=True)
class SubagentTypeDefinition:
    name: str
    description: str
    when_to_use: str = ""
    allowed_tools: tuple[str, ...] | None = None
    exclude_tools: tuple[str, ...] = ()
    supports_background: bool = True


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
