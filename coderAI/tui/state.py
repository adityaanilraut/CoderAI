"""Session and timeline state for the Textual UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

ReasoningEffort = Literal["high", "medium", "low", "none"]


@dataclass
class AgentInfo:
    id: str
    name: str
    parent_id: Optional[str] = None
    status: str = "idle"
    task: Optional[str] = None

    @classmethod
    def from_payload(cls, info: Dict[str, Any]) -> "AgentInfo":
        return cls(
            id=str(info.get("id", "")),
            name=str(info.get("name", "")),
            parent_id=info.get("parentId"),
            status=str(info.get("status", "idle")),
            task=info.get("task"),
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
    available_models: Optional[Dict[str, List[str]]] = None
    available_personas: Optional[List[str]] = None
    available_skills: Optional[List[Dict[str, str]]] = None
    available_mcp_servers: Optional[List[Dict[str, Any]]] = None
    context_files: Optional[List[Dict[str, Any]]] = None
    agents: Dict[str, AgentInfo] = field(default_factory=dict)
    progress: Optional[Dict[str, Any]] = None
    ready: bool = False
    current_tasks: Optional[Dict[str, Any]] = None
    active_persona: Optional[str] = None
