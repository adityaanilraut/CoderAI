"""Autonomous Goal Engine — Domain types, store, and model-facing tool.

Follows DeepSeek Harness dsh-goal specification with round budgeting, revision tracking,
and multi-phase lifecycle management.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from coderai.core.tools.types import ToolExecutionContext, ToolResult, as_str

logger = logging.getLogger(__name__)

DEFAULT_MAX_GOAL_ROUNDS = 20
VALID_GOAL_STATUS = ("running", "paused", "completed", "done", "failed", "pending", "cancelled")


@dataclass
class GoalRef:
    id: str
    revision: int


@dataclass
class Goal:
    id: str
    objective: str
    status: str = "running"  # "running" | "paused" | "completed" | "failed"
    round: int = 1
    max_rounds: int = DEFAULT_MAX_GOAL_ROUNDS
    revision: int = 1
    notes: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "objective": self.objective,
            "status": self.status,
            "round": self.round,
            "max_rounds": self.max_rounds,
            "revision": self.revision,
            "notes": self.notes,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Goal:
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:8]),
            objective=str(data.get("objective") or data.get("title") or ""),
            status=str(data.get("status") if data.get("status") in VALID_GOAL_STATUS else "running"),
            round=int(data.get("round", 1)),
            max_rounds=int(data.get("max_rounds", DEFAULT_MAX_GOAL_ROUNDS)),
            revision=int(data.get("revision", 1)),
            notes=str(data.get("notes") or ""),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
        )


class GoalStore:
    """Thread-safe persistent storage and state coordinator for session goals."""

    def __init__(self, root_dir: str | pathlib.Path = ".coderai/goals") -> None:
        self.root = pathlib.Path(root_dir)
        self._cache: dict[str, list[Goal]] = {}
        self._lock = threading.RLock()

    def _path(self, session_id: str) -> pathlib.Path:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in session_id) or "default"
        return self.root / f"goals-{safe}.json"

    def list(self, session_id: str) -> list[Goal]:
        with self._lock:
            if session_id in self._cache:
                return list(self._cache[session_id])
            path = self._path(session_id)
            if not path.is_file():
                self._cache[session_id] = []
                return []
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                goals = [Goal.from_dict(item) for item in raw if isinstance(item, dict)]
            except Exception:
                goals = []
            self._cache[session_id] = goals
            return list(goals)

    def _save(self, session_id: str, goals: list[Goal]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(session_id)
        data = [g.to_dict() for g in goals]
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        self._cache[session_id] = list(goals)

    def get_active_goal(self, session_id: str) -> Goal | None:
        goals = self.list(session_id)
        for g in reversed(goals):
            if g.status == "running":
                return g
        return None

    def create(
        self,
        session_id: str,
        objective: str,
        max_rounds: int = DEFAULT_MAX_GOAL_ROUNDS,
        notes: str = "",
    ) -> Goal:
        with self._lock:
            goals = self.list(session_id)
            # Pause any currently running goals
            for g in goals:
                if g.status == "running":
                    g.status = "paused"
                    g.updated_at = time.time()
                    g.revision += 1

            goal = Goal(
                id=uuid.uuid4().hex[:8],
                objective=objective.strip(),
                status="running",
                round=1,
                max_rounds=max_rounds,
                revision=1,
                notes=notes.strip(),
            )
            goals.append(goal)
            self._save(session_id, goals)
            return goal

    def add(self, session_id: str, title: str, notes: str = "") -> Goal:
        return self.create(session_id, objective=title, notes=notes)

    def format(self, session_id: str) -> str:
        return self.format_summary(session_id)

    def update(self, session_id: str, goal_id: str, **changes: Any) -> Goal | None:
        with self._lock:
            goals = self.list(session_id)
            for g in goals:
                if g.id != goal_id:
                    continue
                if "objective" in changes and isinstance(changes["objective"], str):
                    g.objective = changes["objective"].strip()
                if "status" in changes and changes["status"] in VALID_GOAL_STATUS:
                    g.status = changes["status"]
                if "round" in changes:
                    g.round = int(changes["round"])
                if "max_rounds" in changes:
                    g.max_rounds = int(changes["max_rounds"])
                if "notes" in changes and isinstance(changes["notes"], str):
                    g.notes = changes["notes"]
                g.revision += 1
                g.updated_at = time.time()
                self._save(session_id, goals)
                return g
            return None

    def advance_round(self, session_id: str, goal_id: str) -> Goal | None:
        with self._lock:
            goals = self.list(session_id)
            for g in goals:
                if g.id == goal_id and g.status == "running":
                    g.round += 1
                    g.revision += 1
                    g.updated_at = time.time()
                    if g.round > g.max_rounds:
                        g.status = "failed"
                        g.notes = (g.notes + "\nExceeded max goal rounds.").strip()
                    self._save(session_id, goals)
                    return g
            return None

    def format_summary(self, session_id: str) -> str:
        goals = self.list(session_id)
        if not goals:
            return "No active goals in current session."

        lines = ["### Session Goals:"]
        for g in goals:
            status_lower = g.status.lower()
            icon = {
                "running": "🚀 RUNNING",
                "completed": "✅ COMPLETED",
                "done": "✅ done",
                "paused": "⏸️ PAUSED",
                "failed": "❌ FAILED",
                "pending": "⏳ pending",
                "cancelled": "🚫 cancelled",
            }.get(status_lower, status_lower)
            lines.append(
                f"- **[{g.id}]** {g.objective} | `{icon}` (Round {g.round}/{g.max_rounds})"
            )
            if g.notes:
                lines.append(f"  *Notes: {g.notes}*")
        return "\n".join(lines)


_global_goal_store: GoalStore | None = None


def get_goal_store(project_root: str = ".") -> GoalStore:
    global _global_goal_store
    if _global_goal_store is None:
        _global_goal_store = GoalStore(root_dir=pathlib.Path(project_root) / ".coderai" / "goals")
    return _global_goal_store


def handle_goal_tool(args: dict[str, Any], context: Any) -> ToolResult:
    """Model-facing tool to manage and progress autonomous goals."""
    action = as_str(args.get("action") or args.get("subcommand", "status")).strip().lower()
    project_root = getattr(context, "project_root", ".") if context else "."
    session_id = getattr(context, "session_id", "default") if context else "default"
    store = get_goal_store(project_root)

    if action == "create":
        objective = as_str(args.get("objective") or args.get("title")).strip()
        if not objective:
            return ToolResult(ok=False, name="goal", error="Missing required 'objective' argument.")
        max_rounds = int(args.get("max_rounds", DEFAULT_MAX_GOAL_ROUNDS))
        notes = as_str(args.get("notes", ""))
        g = store.create(session_id, objective=objective, max_rounds=max_rounds, notes=notes)
        return ToolResult(
            ok=True,
            name="goal",
            output=f"Created goal [{g.id}]: '{g.objective}' (Round {g.round}/{g.max_rounds})",
            metadata=g.to_dict(),
        )

    elif action in ("complete", "done"):
        goal_id = as_str(args.get("goal_id")).strip()
        active = store.get_active_goal(session_id)
        target_id = goal_id or (active.id if active else None)
        if not target_id:
            return ToolResult(ok=False, name="goal", error="No active goal to complete.")
        notes = as_str(args.get("notes", ""))
        updated = store.update(session_id, target_id, status="completed", notes=notes)
        if not updated:
            return ToolResult(ok=False, name="goal", error=f"Goal '{target_id}' not found.")
        return ToolResult(
            ok=True,
            name="goal",
            output=f"Goal [{updated.id}] marked as COMPLETED.",
            metadata=updated.to_dict(),
        )

    elif action == "pause":
        active = store.get_active_goal(session_id)
        if not active:
            return ToolResult(ok=False, name="goal", error="No active running goal to pause.")
        updated = store.update(session_id, active.id, status="paused")
        return ToolResult(
            ok=True,
            name="goal",
            output=f"Goal [{updated.id}] PAUSED.",
            metadata=updated.to_dict() if updated else {},
        )

    elif action == "update":
        goal_id = as_str(args.get("goal_id")).strip()
        active = store.get_active_goal(session_id)
        target_id = goal_id or (active.id if active else None)
        if not target_id:
            return ToolResult(ok=False, name="goal", error="No goal target specified to update.")
        changes = {k: v for k, v in args.items() if k in ("objective", "status", "max_rounds", "notes")}
        updated = store.update(session_id, target_id, **changes)
        if not updated:
            return ToolResult(ok=False, name="goal", error=f"Goal '{target_id}' not found.")
        return ToolResult(
            ok=True,
            name="goal",
            output=f"Goal [{updated.id}] updated.",
            metadata=updated.to_dict(),
        )

    else:  # status / list
        summary = store.format_summary(session_id)
        return ToolResult(
            ok=True,
            name="goal",
            output=summary,
            metadata={"goals": [g.to_dict() for g in store.list(session_id)]},
        )
