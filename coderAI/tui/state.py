"""Session and timeline state for the Textual UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

ReasoningEffort = Literal["high", "medium", "low", "none"]


@dataclass
class AgentInfo:
    id: str
    name: str
    parent_id: Optional[str] = None
    status: str = "idle"
    task: Optional[str] = None
    prompt: Optional[str] = None
    total_tokens: int = 0
    cost_usd: float = 0.0
    ctx_used: int = 0
    ctx_limit: int = 0

    @classmethod
    def from_payload(cls, info: dict[str, Any]) -> "AgentInfo":
        return cls(
            id=str(info.get("id", "")),
            name=str(info.get("name", "")),
            parent_id=info.get("parentId"),
            status=str(info.get("status", "idle")),
            task=info.get("task") or info.get("current_task"),
            prompt=info.get("prompt") or info.get("current_task"),
            total_tokens=int(info.get("tokens") or info.get("total_tokens") or 0),
            cost_usd=float(info.get("costUsd") or info.get("cost_usd") or 0.0),
            ctx_used=int(info.get("ctxUsed") or info.get("ctx_used") or 0),
            ctx_limit=int(info.get("ctxLimit") or info.get("ctx_limit") or 0),
        )


@dataclass
class SessionState:
    thinking: bool = False
    streaming: bool = False
    model: str = ""
    provider: str = ""
    cwd: str = ""
    # None means the workspace has no project execution surface to trust.
    # True/False are shown explicitly when .coderAI hooks/config are present.
    workspace_trusted: Optional[bool] = None
    auto_approve: bool = False
    reasoning: ReasoningEffort = "none"
    verbose: bool = False
    ctx_used: int = 0
    ctx_limit: int = 0
    cost_usd: float = 0.0
    budget_usd: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    iteration: int = 0
    max_iterations: int = 50
    elapsed_s: float = 0.0
    available_models: Optional[dict[str, list[str]]] = None
    available_model_details: Optional[dict[str, dict[str, Any]]] = None
    available_personas: Optional[list[str]] = None
    available_skills: Optional[list[dict[str, str]]] = None
    available_mcp_servers: Optional[list[dict[str, Any]]] = None
    context_files: Optional[list[dict[str, Any]]] = None
    agents: dict[str, AgentInfo] = field(default_factory=dict)
    progress: Optional[dict[str, Any]] = None
    ready: bool = False
    current_tasks: Optional[dict[str, Any]] = None
    active_persona: Optional[str] = None
