"""Structured proposal tool available only during enforced Plan Mode."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from coderAI.core.planning import PlanProposal, PlanStore, validate_plan_proposal
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
        try:
            validate_plan_proposal(proposal)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        self.last_proposal = proposal
        return {
            "success": True,
            "message": "Structured plan submitted for user review.",
            "plan": proposal.model_dump(),
        }


class ExecutionAmendmentInput(BaseModel):
    reason: str = Field(..., min_length=1)
    proposal: PlanProposal


class RequestPlanAmendmentTool(Tool):
    """Persist execution divergence and immediately restore the read-only boundary."""

    name = "request_plan_amendment"
    description = (
        "Stop implementation when the approved plan must change. Supply the reason and complete "
        "replacement plan. This creates an immutable revision, invalidates approval, and switches "
        "the remainder of the turn to read-only Plan Mode until a user approves the amendment."
    )
    parameters_model = ExecutionAmendmentInput
    category = "tasks"
    safe = True

    def __init__(self, agent: Any) -> None:
        self.agent = agent

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        request = ExecutionAmendmentInput.model_validate(kwargs)
        plan_id = getattr(self.agent, "active_plan_id", None)
        approved_revision = getattr(self.agent, "active_plan_revision", None)
        if not plan_id or not approved_revision:
            return {"success": False, "error": "No approved plan execution is active."}
        store = PlanStore(getattr(self.agent.config, "project_root", ".") or ".")
        current = store.load(plan_id)
        if current is None:
            return {"success": False, "error": "The active plan record could not be loaded."}
        if current.approved_revision != approved_revision:
            return {
                "success": False,
                "error": "Active execution is not linked to the plan's approved revision.",
            }
        try:
            amended = store.request_execution_amendment(current, request.proposal, request.reason)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

        # Defense in depth: subsequent schemas and invented calls in this same
        # turn are read-only even before the command/CLI wrapper observes the
        # newly unapproved revision.
        self.agent.plan_mode = True
        self.agent._cached_system_prompt = None
        self.agent._refresh_session_system_prompt()
        return {
            "success": True,
            "message": (
                f"Execution stopped for plan amendment r{amended.revision}; "
                "user review and approval are required before more mutations."
            ),
            "plan_id": amended.plan_id,
            "revision": amended.revision,
            "status": amended.status,
        }
