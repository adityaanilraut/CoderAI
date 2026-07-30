"""Structured proposal tool available only during enforced Plan Mode."""

from __future__ import annotations

from typing import Any

from coderAI.core.planning import PlanProposal
from coderAI.tools.base import Tool


class SubmitPlanTool(Tool):
    name = "submit_plan"
    description = (
        "Submit the complete structured plan after read-only exploration. Call exactly once. "
        "The plan must be implementation-ready: concrete success criteria, scoped steps, files, "
        "checks, risks, and any genuinely unanswered product decisions. This tool proposes a plan; "
        "it never approves or executes it."
    )
    parameters_model = PlanProposal
    category = "tasks"
    safe = True
    last_proposal: PlanProposal | None

    def __init__(self) -> None:
        self.last_proposal = None

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        proposal = PlanProposal.model_validate(kwargs)
        ids = [step.id for step in proposal.steps]
        if len(ids) != len(set(ids)):
            return {"success": False, "error": "Plan step IDs must be unique."}
        known = set(ids)
        invalid = {dep for step in proposal.steps for dep in step.depends_on if dep not in known}
        if invalid:
            return {
                "success": False,
                "error": "Unknown step dependencies: " + ", ".join(sorted(invalid)),
            }
        if any(not criterion.strip() for criterion in proposal.success_criteria):
            return {"success": False, "error": "Success criteria may not be blank."}

        graph = {step.id: step.depends_on for step in proposal.steps}
        visiting: set[str] = set()
        visited: set[str] = set()

        def _has_cycle(step_id: str) -> bool:
            if step_id in visiting:
                return True
            if step_id in visited:
                return False
            visiting.add(step_id)
            if any(_has_cycle(dep) for dep in graph[step_id]):
                return True
            visiting.remove(step_id)
            visited.add(step_id)
            return False

        if any(_has_cycle(step_id) for step_id in ids):
            return {"success": False, "error": "Plan step dependencies contain a cycle."}
        self.last_proposal = proposal
        return {
            "success": True,
            "message": "Structured plan submitted for user review.",
            "plan": proposal.model_dump(),
        }
