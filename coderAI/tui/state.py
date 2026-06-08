"""Session and timeline state for the Textual UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

ReasoningEffort = Literal["high", "medium", "low", "none"]


@dataclass
class AgentInfo:
    id: str
    name: str
    role: Optional[str] = None
    parent_id: Optional[str] = None
    status: str = "idle"
    task: Optional[str] = None
    tool: Optional[str] = None
    model: Optional[str] = None
    tokens: int = 0
    cost_usd: float = 0.0
    ctx_used: int = 0
    ctx_limit: int = 0
    elapsed_ms: int = 0
    depth: int = 0

    @classmethod
    def from_payload(cls, info: Dict[str, Any]) -> "AgentInfo":
        return cls(
            id=str(info.get("id", "")),
            name=str(info.get("name", "")),
            role=info.get("role"),
            parent_id=info.get("parentId"),
            status=str(info.get("status", "idle")),
            task=info.get("task"),
            tool=info.get("tool"),
            model=info.get("model"),
            tokens=int(info.get("tokens") or 0),
            cost_usd=float(info.get("costUsd") or 0),
            ctx_used=int(info.get("ctxUsed") or 0),
            ctx_limit=int(info.get("ctxLimit") or 0),
            elapsed_ms=int(info.get("elapsedMs") or 0),
            depth=int(info.get("depth") or 0),
        )


@dataclass
class SessionState:
    connected: bool = False
    thinking: bool = False
    streaming: bool = False
    model: str = ""
    provider: str = ""
    cwd: str = ""
    version: str = ""
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
    available_models: Optional[Dict[str, List[str]]] = None
    available_personas: Optional[List[str]] = None
    available_skills: Optional[List[Dict[str, str]]] = None
    context_files: Optional[List[Dict[str, Any]]] = None
    agents: Dict[str, AgentInfo] = field(default_factory=dict)
    progress: Optional[Dict[str, Any]] = None
    session_started_at: Optional[float] = None
    ready: bool = False
    current_plan: Optional[Dict[str, Any]] = None
    active_persona: Optional[str] = None
