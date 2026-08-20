"""Session goals store and model-facing goal tool (/goal)."""

from __future__ import annotations

import builtins
import json
import pathlib
import uuid
from dataclasses import dataclass, field
from typing import Any

from coderai.core.tools.types import ToolExecutionContext, ToolResult, as_str

VALID_GOAL_STATUS = ("pending", "in_progress", "done", "cancelled")


@dataclass
class Goal:
    id: str
    title: str
    status: str = "pending"
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "status": self.status, "notes": self.notes}


@dataclass
class GoalStore:
    root: pathlib.Path
    _cache: dict[str, builtins.list[Goal]] = field(default_factory=dict)

    def _path(self, session_id: str) -> pathlib.Path:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in session_id) or "default"
        return self.root / f"goals-{safe}.json"

    def list(self, session_id: str) -> builtins.list[Goal]:
        if session_id in self._cache:
            return builtins.list(self._cache[session_id])
        path = self._path(session_id)
        if not path.is_file():
            self._cache[session_id] = []
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._cache[session_id] = []
            return []
        goals: builtins.list[Goal] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict) or not item.get("title"):
                continue
            item_status = item.get("status")
            status: str = str(item_status) if item_status in VALID_GOAL_STATUS else "pending"
            goals.append(
                Goal(
                    id=str(item.get("id") or uuid.uuid4().hex[:8]),
                    title=str(item["title"]),
                    status=status,
                    notes=str(item.get("notes") or ""),
                )
            )
        self._cache[session_id] = goals
        return builtins.list(goals)

    def _save(self, session_id: str, goals: builtins.list[Goal]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._path(session_id).write_text(
            json.dumps([g.to_dict() for g in goals], indent=2) + "\n",
            encoding="utf-8",
        )
        self._cache[session_id] = builtins.list(goals)

    def add(self, session_id: str, title: str, notes: str = "") -> Goal:
        goals = self.list(session_id)
        goal = Goal(id=uuid.uuid4().hex[:8], title=title.strip(), notes=notes.strip())
        goals.append(goal)
        self._save(session_id, goals)
        return goal

    def update(self, session_id: str, goal_id: str, **changes: Any) -> Goal | None:
        goals = self.list(session_id)
        for goal in goals:
            if goal.id != goal_id:
                continue
            if (
                "title" in changes
                and isinstance(changes["title"], str)
                and changes["title"].strip()
            ):
                goal.title = changes["title"].strip()
            if "status" in changes and changes["status"] in VALID_GOAL_STATUS:
                goal.status = changes["status"]
            if "notes" in changes and isinstance(changes["notes"], str):
                goal.notes = changes["notes"]
            self._save(session_id, goals)
            return goal
        return None

    def format(self, session_id: str) -> str:
        goals = self.list(session_id)
        if not goals:
            return "(no goals)"
        lines = []
        for goal in goals:
            note = f" — {goal.notes}" if goal.notes else ""
            lines.append(f"- [{goal.status}] {goal.id}: {goal.title}{note}")
        return "\n".join(lines)


_stores: dict[str, GoalStore] = {}


def get_goal_store(project_root: str) -> GoalStore:
    key = str(pathlib.Path(project_root).resolve())
    store = _stores.get(key)
    if store is None:
        store = GoalStore(root=pathlib.Path(key) / ".coderai")
        _stores[key] = store
    return store


def handle_goal_tool(args: dict[str, Any], context: ToolExecutionContext | Any) -> ToolResult:
    action = as_str(args.get("action") or "list").strip().lower()
    session_id = str(getattr(context, "session_id", "") or "")
    store = get_goal_store(str(getattr(context, "project_root", ".") or "."))
    if action == "list":
        return ToolResult(ok=True, name="goal", output=store.format(session_id))
    if action == "add":
        title = as_str(args.get("title")).strip()
        if not title:
            return ToolResult(ok=False, name="goal", error="title is required to add a goal.")
        goal = store.add(session_id, title, as_str(args.get("notes")))
        return ToolResult(
            ok=True,
            name="goal",
            output=f"Added goal {goal.id}: {goal.title}",
            metadata=goal.to_dict(),
        )
    if action in ("update", "done", "cancel", "start"):
        goal_id = as_str(args.get("goal_id") or args.get("id")).strip()
        if not goal_id:
            return ToolResult(ok=False, name="goal", error="goal_id is required.")
        status = {
            "done": "done",
            "cancel": "cancelled",
            "start": "in_progress",
        }.get(action)
        if action == "update":
            status = as_str(args.get("status")).strip() or None
        updated = store.update(
            session_id,
            goal_id,
            status=status,
            title=args.get("title"),
            notes=args.get("notes"),
        )
        if not updated:
            return ToolResult(ok=False, name="goal", error=f"Unknown goal '{goal_id}'.")
        return ToolResult(
            ok=True,
            name="goal",
            output=f"Updated goal {updated.id} [{updated.status}]: {updated.title}",
            metadata=updated.to_dict(),
        )
    return ToolResult(
        ok=False,
        name="goal",
        error=f"Unknown action '{action}'. Use list, add, update, done, cancel, start.",
    )
