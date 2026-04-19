"""Task management tool for persistent planning across invocations."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .base import Tool
from ..config import config_manager


def get_tasks_file(project_root: str = ".") -> Path:
    """Get the path to the tasks file for the current project or fallback to global."""
    project_dir = Path(project_root).resolve() / ".coderAI"
    if not project_dir.exists():
        project_dir.mkdir(exist_ok=True, parents=True)
    return project_dir / "tasks.json"


class ManageTasksParams(BaseModel):
    action: str = Field(
        ...,
        description=(
            "Action to perform: 'list', 'add', 'update', 'start', "
            "'complete', 'delete', or 'clear'"
        ),
    )
    task_id: Optional[int] = Field(
        None, description="Task ID (required for update, start, complete, delete)"
    )
    title: Optional[str] = Field(
        None, description="Task title (required for add, optional for update)"
    )
    description: Optional[str] = Field(None, description="Task details (optional)")
    priority: Optional[str] = Field(
        None,
        description="Task priority: 'high', 'medium', or 'low' (default: medium)",
    )
    project_root: str = Field(
        ".", description="Project root directory (default: current directory)"
    )


# Priority ordering for sort
_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


class ManageTasksTool(Tool):
    """Tool for managing a persistent task/TODO list."""

    name = "manage_tasks"
    description = (
        "Manage a persistent task/TODO list with priorities. Use this to plan "
        "and track progress across multiple steps. Actions: 'list' (shows all "
        "tasks grouped by status), 'add' (requires title; optional priority), "
        "'start' (marks task as in-progress; requires task_id), 'complete' "
        "(requires task_id), 'update' (requires task_id), 'delete' (requires "
        "task_id), 'clear' (removes completed tasks)."
    )
    parameters_model = ManageTasksParams
    is_read_only = False

    async def execute(
        self,
        action: str,
        task_id: Optional[int] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        priority: Optional[str] = None,
        project_root: str = ".",
    ) -> Dict[str, Any]:
        """Execute task management action."""
        try:
            tasks_file = get_tasks_file(project_root)
            tasks = self._load_tasks(tasks_file)

            if action == "list":
                return self._format_tasks(tasks)

            elif action == "add":
                if not title:
                    return {
                        "success": False,
                        "error": "Title is required for 'add' action",
                    }

                pri = (priority or "medium").lower()
                if pri not in _PRIORITY_ORDER:
                    pri = "medium"

                new_id = 1 if not tasks else max(t["id"] for t in tasks) + 1
                new_task = {
                    "id": new_id,
                    "title": title,
                    "description": description or "",
                    "priority": pri,
                    "status": "pending",
                    "created_at": datetime.now().isoformat(),
                    "completed_at": None,
                }
                tasks.append(new_task)
                self._save_tasks(tasks_file, tasks)
                return {
                    "success": True,
                    "message": f"Added task #{new_id} [{pri}]: {title}",
                    "task": new_task,
                }

            elif action in ("update", "start", "complete", "delete"):
                if task_id is None:
                    return {
                        "success": False,
                        "error": f"task_id is required for '{action}' action",
                    }

                task_idx = next(
                    (i for i, t in enumerate(tasks) if t["id"] == task_id), None
                )
                if task_idx is None:
                    return {
                        "success": False,
                        "error": f"Task #{task_id} not found",
                    }

                if action == "delete":
                    deleted = tasks.pop(task_idx)
                    self._save_tasks(tasks_file, tasks)
                    return {
                        "success": True,
                        "message": f"Deleted task #{task_id}: {deleted['title']}",
                    }

                elif action == "start":
                    tasks[task_idx]["status"] = "in_progress"
                    self._save_tasks(tasks_file, tasks)
                    return {
                        "success": True,
                        "message": f"Started task #{task_id}: {tasks[task_idx]['title']}",
                    }

                elif action == "complete":
                    tasks[task_idx]["status"] = "completed"
                    tasks[task_idx]["completed_at"] = datetime.now().isoformat()
                    self._save_tasks(tasks_file, tasks)
                    return {
                        "success": True,
                        "message": f"Completed task #{task_id}: {tasks[task_idx]['title']}",
                    }

                elif action == "update":
                    if title:
                        tasks[task_idx]["title"] = title
                    if description is not None:
                        tasks[task_idx]["description"] = description
                    if priority:
                        pri = priority.lower()
                        if pri in _PRIORITY_ORDER:
                            tasks[task_idx]["priority"] = pri
                    self._save_tasks(tasks_file, tasks)
                    return {
                        "success": True,
                        "message": f"Updated task #{task_id}",
                        "task": tasks[task_idx],
                    }

            elif action == "clear":
                before = len(tasks)
                tasks = [t for t in tasks if t["status"] != "completed"]
                cleared = before - len(tasks)
                self._save_tasks(tasks_file, tasks)
                return {
                    "success": True,
                    "message": f"Cleared {cleared} completed task(s)",
                }

            else:
                return {"success": False, "error": f"Unknown action: {action}"}

        except Exception as e:
            return {"success": False, "error": str(e)}

    def _load_tasks(self, filepath: Path) -> List[Dict[str, Any]]:
        if not filepath.exists():
            return []
        try:
            with open(filepath, "r") as f:
                tasks = json.load(f)
            # Backfill priority for tasks created before this field existed
            for t in tasks:
                if "priority" not in t:
                    t["priority"] = "medium"
            return tasks
        except (json.JSONDecodeError, IOError):
            return []

    def _save_tasks(self, filepath: Path, tasks: List[Dict[str, Any]]) -> None:
        with open(filepath, "w") as f:
            json.dump(tasks, f, indent=2)

    def _format_tasks(self, tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not tasks:
            return {"success": True, "message": "No tasks found", "tasks": []}

        def _sort_key(t: Dict[str, Any]) -> int:
            return _PRIORITY_ORDER.get(t.get("priority", "medium"), 1)

        in_progress = sorted(
            [t for t in tasks if t.get("status") == "in_progress"], key=_sort_key
        )
        pending = sorted(
            [t for t in tasks if t.get("status") == "pending"], key=_sort_key
        )
        completed = [t for t in tasks if t.get("status") == "completed"]

        return {
            "success": True,
            "summary": (
                f"{len(in_progress)} in-progress, "
                f"{len(pending)} pending, "
                f"{len(completed)} completed"
            ),
            "in_progress": in_progress,
            "pending": pending,
            "completed": completed,
            "total": len(tasks),
        }
