"""Planning tool for structured plan-and-execute workflows."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .base import Tool
from ..config import config_manager

_PLANS_DIR = ".coderAI"


def _get_plan_file(project_root: str = ".") -> Path:
    plan_dir = Path(project_root).resolve() / _PLANS_DIR
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir / "current_plan.json"


class PlanParams(BaseModel):
    action: str = Field(
        ...,
        description=(
            "Action: 'create' (make a new plan), 'show' (display current plan), "
            "'advance' (mark current step done and move to next), "
            "'update_step' (modify a step), 'clear' (remove the plan)."
        ),
    )
    title: Optional[str] = Field(
        None,
        description="Plan title (required for 'create').",
    )
    steps: Optional[List[str]] = Field(
        None,
        description="List of step descriptions (required for 'create').",
    )
    step_index: Optional[int] = Field(
        None,
        description="0-based step index (for 'update_step').",
    )
    new_description: Optional[str] = Field(
        None,
        description="New description for the step (for 'update_step').",
    )


class CreatePlanTool(Tool):
    """Structured planning tool for multi-step task execution."""

    name = "plan"
    description = (
        "Create and manage a structured execution plan. Before starting a complex task, "
        "use action='create' with a title and list of steps. Use action='show' to display "
        "the plan, action='advance' to mark the current step done and proceed, and "
        "action='clear' to remove the plan. This helps organize multi-step work and "
        "track progress."
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

                # Preflight: verify the target directory is a real project
                from ..safeguards import project_sanity_check
                check = project_sanity_check(config.project_root)
                if not check["is_valid_project"]:
                    reasons = "; ".join(check["reasons"])
                    return {
                        "success": False,
                        "error": (
                            f"Cannot create plan: target directory does not "
                            f"appear to be a valid project. {reasons}"
                        ),
                        "error_code": "empty_project",
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
                with open(plan_file, "w") as f:
                    json.dump(plan, f, indent=2)

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

                with open(plan_file, "w") as f:
                    json.dump(plan, f, indent=2)

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
                with open(plan_file, "w") as f:
                    json.dump(plan, f, indent=2)

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
