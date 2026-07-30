"""Task management tool for persistent planning across invocations."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from coderAI.types.tool_error_codes import ToolErrorCode
from coderAI.system.events import event_emitter
from coderAI.system.fsperms import atomic_write_json
from coderAI.tools.base import Tool
from coderAI.tools.filesystem import ProjectPathError, resolve_under_project


def get_tasks_file(project_root: Optional[str] = None) -> Path:
    """Get the guarded tasks path for the active project."""
    requested = Path(project_root or ".") / ".coderAI" / "tasks.json"
    tasks_file = resolve_under_project(
        requested,
        operation="manage tasks",
        check_protected=True,
        reject_symlink=True,
    )
    project_dir = tasks_file.parent
    if not project_dir.exists():
        project_dir.mkdir(exist_ok=True, parents=True)
    return tasks_file


class ManageTasksParams(BaseModel):
    action: str = Field(
        ...,
        description=(
            "Action to perform: 'list', 'add', 'update', 'start', 'complete', 'delete', or 'clear'"
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


# Priority ordering for sort
_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
_TASK_STATUSES = {"pending", "in_progress", "completed"}


class TasksDataError(ValueError):
    """Raised when persisted task data cannot be safely interpreted."""

    def __init__(self, message: str, code: ToolErrorCode = ToolErrorCode.VALIDATION):
        super().__init__(message)
        self.code = code


class ManageTasksTool(Tool):
    """Tool for managing a persistent task/TODO list."""

    name = "manage_tasks"
    description = (
        "Persistent TODO / plan checklist (file-backed at `.coderAI/tasks.json`; "
        "survives across turns). Use for multi-step work: add ordered tasks, "
        "start/complete them as you go, and list status before changing course. "
        "Skip for trivial single-step work. Actions: 'list' (groups by status), "
        "'add' (requires title; optional priority), 'start' (requires task_id), "
        "'complete' (requires task_id), 'update' (requires task_id), 'delete' "
        "(requires task_id), 'clear' (removes completed tasks)."
    )
    category = "tasks"
    parameters_model = ManageTasksParams
    is_read_only = False
    # Mutates only the agent's own task list (`.coderAI/tasks.json`) — no
    # arbitrary filesystem, network, or shell effect — so it runs without
    # per-call confirmation.
    safe = True

    async def execute(  # type: ignore[override]
        self,
        action: str,
        task_id: Optional[int] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        priority: Optional[str] = None,
        project_root: Optional[str] = None,
    ) -> dict[str, Any]:
        """Execute task management action."""
        try:
            # Kept only for typed internal callers predating the model schema.
            # The value is intentionally ignored; task storage is always active-project scoped.
            _ = project_root
            tasks_file = get_tasks_file()
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

                task_idx = next((i for i, t in enumerate(tasks) if t["id"] == task_id), None)
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
                else:
                    return {
                        "success": False,
                        "error": f"Invalid sub-action: {action}",
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

        except ProjectPathError as e:
            return e.as_result()
        except TasksDataError as e:
            return {
                "success": False,
                "error": f"Task data is invalid; refusing to modify it: {e}",
                "error_code": e.code,
            }
        except Exception as e:
            return {"success": False, "error": str(e), "error_code": ToolErrorCode.TOOL_ERROR}

    def _load_tasks(self, filepath: Path) -> list[dict[str, Any]]:
        if not filepath.exists():
            return []
        try:
            with open(filepath, "r") as f:
                tasks = json.load(f)
        except json.JSONDecodeError as exc:
            raise TasksDataError("malformed JSON", ToolErrorCode.PARSE_ERROR) from exc
        except OSError as exc:
            raise TasksDataError(f"could not read tasks file: {exc}", ToolErrorCode.IO) from exc

        if not isinstance(tasks, list):
            raise TasksDataError(f"expected a JSON array, got {type(tasks).__name__}")

        validated: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        for position, raw in enumerate(tasks):
            if not isinstance(raw, dict):
                raise TasksDataError(f"task at position {position} is not an object")
            task = dict(raw)
            task_id = task.get("id")
            if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id < 1:
                raise TasksDataError(f"task at position {position} has an invalid id")
            if task_id in seen_ids:
                raise TasksDataError(f"duplicate task id {task_id}")
            seen_ids.add(task_id)
            if not isinstance(task.get("title"), str) or not task["title"].strip():
                raise TasksDataError(f"task #{task_id} has an invalid title")
            if task.get("status") not in _TASK_STATUSES:
                raise TasksDataError(f"task #{task_id} has an invalid status")

            # Backfill fields introduced after the original task format.
            task.setdefault("priority", "medium")
            task.setdefault("description", "")
            task.setdefault("created_at", "")
            task.setdefault("completed_at", None)
            if task["priority"] not in _PRIORITY_ORDER:
                raise TasksDataError(f"task #{task_id} has an invalid priority")
            if not isinstance(task["description"], str):
                raise TasksDataError(f"task #{task_id} has an invalid description")
            if not isinstance(task["created_at"], str):
                raise TasksDataError(f"task #{task_id} has an invalid created_at")
            if task["completed_at"] is not None and not isinstance(task["completed_at"], str):
                raise TasksDataError(f"task #{task_id} has an invalid completed_at")
            validated.append(task)
        return validated

    def _save_tasks(self, filepath: Path, tasks: list[dict[str, Any]]) -> None:
        atomic_write_json(filepath, tasks, fsync=True)
        event_emitter.emit("tasks_update", tasks=tasks)

    def _format_tasks(self, tasks: list[dict[str, Any]]) -> dict[str, Any]:
        if not tasks:
            return {"success": True, "message": "No tasks found", "tasks": []}

        def _sort_key(t: dict[str, Any]) -> int:
            return _PRIORITY_ORDER.get(t.get("priority", "medium"), 1)

        in_progress = sorted([t for t in tasks if t.get("status") == "in_progress"], key=_sort_key)
        pending = sorted([t for t in tasks if t.get("status") == "pending"], key=_sort_key)
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
