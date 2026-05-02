"""Planning tool for structured plan-and-execute workflows."""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from .base import Tool
from ..config import config_manager

_PLANS_DIR = ".coderAI"


def _atomic_write_json(filepath: Path, data: dict) -> None:
    fd, tmp_path = tempfile.mkstemp(
        dir=str(filepath.parent), prefix=".plan-", suffix=".json.tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, filepath)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
def _get_plan_file(project_root: str = ".") -> Path:
    plan_dir = Path(project_root).resolve() / _PLANS_DIR
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir / "current_plan.json"


class PlanParams(BaseModel):
    action: Literal["create", "show", "advance", "update_step", "clear"] = Field(
        ...,
        description=(
            "Action: 'create' (make a new plan), 'show' (display current plan), "
            "'advance' (mark current step done and move to next), "
            "'update_step' (modify a step), 'clear' (remove the plan)."
        ),
    )
    title: Optional[str] = Field(
        default=None,
        description="Plan title (required for 'create').",
    )
    steps: Optional[List[str]] = Field(
        default=None,
        description="List of step descriptions (required for 'create').",
    )
    step_index: Optional[int] = Field(
        default=None,
        description="0-based step index (for 'update_step').",
    )
    new_description: Optional[str] = Field(
        default=None,
        description="New description for the step (for 'update_step').",
    )


class CreatePlanTool(Tool):
    """Structured planning tool for multi-step task execution."""

    name = "plan"
    description = (
        "Create and manage a structured execution plan. Use this when work has several "
        "ordered steps and you need to track progress across them. Do not use it for "
        "single-step tasks or scratch notes; use notepad for that. Example: action='create', "
        "title='Refactor auth flow', steps=['Map current flow', 'Patch backend', 'Add tests']."
    )
    parameters_model = PlanParams
    is_read_only = False

    async def execute(
        self,
        action: str,
        title: Optional[str] = None,
        steps: Optional[List[str]] = None,
        step_index: Optional[int] = None,
        new_description: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            config = config_manager.load_project_config(".")
            plan_file = _get_plan_file(config.project_root)

            if action == "create":
                if not title:
                    return {"success": False, "error": "title is required for 'create'."}
                if not steps or len(steps) == 0:
                    return {"success": False, "error": "steps list is required for 'create'."}

                # Verify the target directory exists and is a directory
                from pathlib import Path
                target = Path(config.project_root).resolve()
                if not target.exists():
                    return {
                        "success": False,
                        "error": f"Cannot create plan: directory does not exist: {target}",
                        "error_code": "missing_directory",
                    }
                if not target.is_dir():
                    return {
                        "success": False,
                        "error": f"Cannot create plan: path is not a directory: {target}",
                        "error_code": "not_a_directory",
                    }

                plan = {
                    "title": title,
                    "created_at": datetime.now().isoformat(),
                    "current_step": 0,
                    "steps": [
                        {"index": i, "description": desc, "status": "pending"}
                        for i, desc in enumerate(steps)
                    ],
                }
                _atomic_write_json(plan_file, plan)

                return {
                    "success": True,
                    "message": f"Plan '{title}' created with {len(steps)} steps.",
                    "plan": plan,
                }

            elif action == "show":
                if not plan_file.exists():
                    return {"success": True, "message": "No active plan.", "plan": None}

                with open(plan_file, "r") as f:
                    plan = json.load(f)

                completed = sum(1 for s in plan["steps"] if s["status"] == "done")
                total = len(plan["steps"])
                current = plan.get("current_step", 0)
                current_desc = (
                    plan["steps"][current]["description"]
                    if current < total
                    else "All steps completed"
                )

                return {
                    "success": True,
                    "plan": plan,
                    "progress": f"{completed}/{total} steps completed",
                    "current_step": current_desc,
                }

            elif action == "advance":
                if not plan_file.exists():
                    return {"success": False, "error": "No active plan to advance."}

                with open(plan_file, "r") as f:
                    plan = json.load(f)

                current = plan.get("current_step", 0)
                if current >= len(plan["steps"]):
                    return {
                        "success": True,
                        "message": "Plan already completed!",
                        "plan": plan,
                    }

                plan["steps"][current]["status"] = "done"
                plan["steps"][current]["completed_at"] = datetime.now().isoformat()
                plan["current_step"] = current + 1

                _atomic_write_json(plan_file, plan)

                next_step = (
                    plan["steps"][current + 1]["description"]
                    if current + 1 < len(plan["steps"])
                    else "All steps completed!"
                )

                return {
                    "success": True,
                    "message": f"Step {current} completed: {plan['steps'][current]['description']}",
                    "next_step": next_step,
                    "progress": f"{current + 1}/{len(plan['steps'])} done",
                }

            elif action == "update_step":
                if step_index is None:
                    return {"success": False, "error": "step_index is required for 'update_step'."}
                if not new_description:
                    return {"success": False, "error": "new_description is required."}

                if not plan_file.exists():
                    return {"success": False, "error": "No active plan."}

                with open(plan_file, "r") as f:
                    plan = json.load(f)

                if step_index < 0 or step_index >= len(plan["steps"]):
                    return {"success": False, "error": f"Invalid step_index: {step_index}"}

                plan["steps"][step_index]["description"] = new_description
                _atomic_write_json(plan_file, plan)

                return {
                    "success": True,
                    "message": f"Step {step_index} updated.",
                    "step": plan["steps"][step_index],
                }

            elif action == "clear":
                if plan_file.exists():
                    plan_file.unlink()
                return {"success": True, "message": "Plan cleared."}

            else:
                return {
                    "success": False,
                    "error": f"Unknown action: {action}. Use 'create', 'show', 'advance', 'update_step', or 'clear'.",
                }

        except Exception as e:
            return {"success": False, "error": str(e)}
