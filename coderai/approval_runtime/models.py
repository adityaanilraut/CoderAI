# Ported from coderai/core/approval.py - kimi structure (approval_runtime/models.py).
"""Coordinated approval runtime (Kimi ``approval_runtime/`` parity, slim).

Foreground turns and background agents route approval decisions through one
registry so the UI channel sees every request exactly once. Sources carry
``(kind, id, agent_id)`` so cancellation can target a whole subtree
(``cancel_by_source``). Responses resolve an ``asyncio`` future — no wire
dependency; wire/ACP adapters live in later phases.
"""

from __future__ import annotations

import asyncio
import contextvars
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

ApprovalResponseKind = Literal["approve", "approve_for_session", "reject"]
ApprovalSourceKind = Literal["foreground_turn", "background_agent"]
ApprovalStatus = Literal["pending", "resolved", "cancelled"]
ApprovalEventKind = Literal["request_created", "request_resolved"]


@dataclass(frozen=True, slots=True, kw_only=True)
class ApprovalSource:
    kind: ApprovalSourceKind
    id: str
    agent_id: str | None = None
    subagent_type: str | None = None


@dataclass(slots=True, kw_only=True)
class ApprovalRequestRecord:
    id: str
    tool_call_id: str
    action: str
    description: str
    source: ApprovalSource
    sender: str = ""
    display: list[Any] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    status: ApprovalStatus = "pending"
    resolved_at: float | None = None
    response: ApprovalResponseKind | None = None
    feedback: str = ""
    approved_via_session_cache: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class ApprovalEvent:
    kind: ApprovalEventKind
    request: ApprovalRequestRecord
