"""Planning tool for structured plan-and-execute workflows."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from coderAI.core.tool_error_codes import ToolErrorCode
from coderAI.tools.base import Tool
from coderAI.system.config import config_manager
from coderAI.system.events import event_emitter
from coderAI.system.fsperms import atomic_write_json
from coderAI.system.project_layout import find_dot_coderai_subdir

_PLANS_DIR = ".coderAI"


def _atomic_write_json(filepath: Path, data: dict) -> None:
    atomic_write_json(filepath, data, fsync=True)


def _get_plan_file(project_root: str = ".") -> Path:
    plan_dir = find_dot_coderai_subdir("", project_root)
    if plan_dir is None:
        plan_dir = Path(project_root).resolve() / _PLANS_DIR
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir / "current_plan.json"


class PlanParams(BaseModel):
    action: Literal["create", "show", "status", "advance", "update_step", "clear"] = Field(
        ...,
        description=(
            "Action: 'create' (make a new plan), 'show' (display the full plan), "
            "'status' (cheap progress snapshot: current/next step + counts), "
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
        "Create and manage a structured execution plan that the user can follow along with. "
        "Use this when work has 3+ ordered steps. Actions: 'create' (once, at the start), "
        "'status' (cheap progress check between steps), 'advance' (after finishing a step), "
        "'show' (full plan), 'update_step' (amend mid-flight), 'clear'. Do not use it for "
        "single-step tasks; use notepad for scratch notes. Example: action='create', "
        "title='Refactor auth flow', steps=['Map current flow', 'Patch backend', 'Add tests']."
    )
    category = "planning"
    parameters_model = PlanParams
    is_read_only = False
    # Mutates only the agent's own in-session plan state — no filesystem,
    # network, or shell effect — so it runs without per-call confirmation.
    safe = True

    def __init__(self) -> None:
        super().__init__()
        self.project_root = "."

    async def execute(  # type: ignore[override]
        self,
        action: str,
        title: Optional[str] = None,
        steps: Optional[List[str]] = None,
        step_index: Optional[int] = None,
        new_description: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            config = config_manager.load_project_config(self.project_root)
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
                        "error_code": ToolErrorCode.MISSING_DIRECTORY,
                    }
                if not target.is_dir():
                    return {
                        "success": False,
                        "error": f"Cannot create plan: path is not a directory: {target}",
                        "error_code": ToolErrorCode.NOT_A_DIRECTORY,
                    }

                # schema_version=2 introduced when the `status` action landed.
                # Plans written before this (no `schema_version`) are treated as
                # v1 and remain readable — no migration needed because every
                # other branch only touches fields that have always existed.
                plan = {
                    "schema_version": 2,
                    "title": title,
                    "created_at": datetime.now().isoformat(),
                    "current_step": 0,
                    "steps": [
                        {"index": i, "description": desc, "status": "pending"}
                        for i, desc in enumerate(steps)
                    ],
                }
                _atomic_write_json(plan_file, plan)
                event_emitter.emit("plan_update", plan=plan)

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

            elif action == "status":
                # Cheap progress snapshot for between-step checks. Returns only
                # counts + current/next descriptions, not the full step list,
                # so the agent can call it frequently without bloating context.
                if not plan_file.exists():
                    return {
                        "success": True,
                        "done": True,
                        "total_steps": 0,
                        "completed_steps": 0,
                        "current_step_index": 0,
                        "current_step_description": "",
                        "next_step_description": None,
                        "title": "",
                        "message": "No active plan.",
                    }

                with open(plan_file, "r") as f:
                    plan = json.load(f)

                plan_steps = plan.get("steps", [])
                total = len(plan_steps)
                completed = sum(
                    1 for s in plan_steps if isinstance(s, dict) and s.get("status") == "done"
                )
                current = plan.get("current_step", 0)
                done = current >= total
                if done:
                    current_desc = "All steps completed"
                    next_desc = None
                else:
                    current_step_item = plan_steps[current]
                    current_desc = (
                        current_step_item.get("description", "")
                        if isinstance(current_step_item, dict)
                        else ""
                    )
                    if current + 1 < total:
                        next_step_item = plan_steps[current + 1]
                        next_desc = (
                            next_step_item.get("description", "")
                            if isinstance(next_step_item, dict)
                            else ""
                        )
                    else:
                        next_desc = None

                return {
                    "success": True,
                    "done": done,
                    "total_steps": total,
                    "completed_steps": completed,
                    "current_step_index": current,
                    "current_step_description": current_desc,
                    "next_step_description": next_desc,
                    "title": plan.get("title", ""),
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
                event_emitter.emit("plan_update", plan=plan)

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
                event_emitter.emit("plan_update", plan=plan)

                return {
                    "success": True,
                    "message": f"Step {step_index} updated.",
                    "step": plan["steps"][step_index],
                }

            elif action == "clear":
                if plan_file.exists():
                    plan_file.unlink()
                event_emitter.emit("plan_update", plan=None)
                return {"success": True, "message": "Plan cleared."}

            else:
                return {
                    "success": False,
                    "error": f"Unknown action: {action}. Use 'create', 'show', 'status', 'advance', 'update_step', or 'clear'.",
                }

        except Exception as e:
            return {"success": False, "error": str(e)}
