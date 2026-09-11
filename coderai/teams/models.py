"""Data models for Agent Teams and Swarm Coordination."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TeamMessage:
    """A direct message or notification sent between teammates."""

    message_id: str = field(default_factory=lambda: f"msg_{uuid.uuid4().hex[:8]}")
    sender: str = "coordinator"
    recipient: str = "all"
    content: str = ""
    timestamp: float = field(default_factory=time.time)
    task_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "sender": self.sender,
            "recipient": self.recipient,
            "content": self.content,
            "timestamp": self.timestamp,
            "task_id": self.task_id,
        }


@dataclass
class TeamTask:
    """A shared task on the team task board."""

    task_id: str
    title: str
    description: str
    assigned_to: str | None = None
    status: str = "pending"  # "pending" | "in_progress" | "completed" | "blocked" | "failed"
    priority: str = "medium"  # "low" | "medium" | "high" | "critical"
    dependencies: list[str] = field(default_factory=list)
    result: str | None = None
    notes: str | None = None
    revision: int = 1
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "description": self.description,
            "assigned_to": self.assigned_to,
            "status": self.status,
            "priority": self.priority,
            "dependencies": self.dependencies,
            "result": self.result,
            "notes": self.notes,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class Teammate:
    """A dedicated role-based teammate agent in the swarm."""

    teammate_id: str
    name: str
    role: str  # "architect" | "coder" | "reviewer" | "tester" | "researcher" | "coordinator"
    mode: str = "general"  # "general" | "read_only"
    status: str = "idle"  # "idle" | "working" | "completed" | "failed" | "interrupted"
    system_prompt: str | None = None
    allowed_tools: list[str] | None = None
    inbox: list[TeamMessage] = field(default_factory=list)
    outbox: list[TeamMessage] = field(default_factory=list)
    current_task_id: str | None = None
    last_report: str | None = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "teammate_id": self.teammate_id,
            "name": self.name,
            "role": self.role,
            "mode": self.mode,
            "status": self.status,
            "current_task_id": self.current_task_id,
            "inbox_count": len(self.inbox),
            "outbox_count": len(self.outbox),
            "last_report": self.last_report,
            "created_at": self.created_at,
        }
