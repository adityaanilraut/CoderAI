"""Versioned, project-scoped planning artifacts for real Plan Mode."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from coderAI.system.fsperms import atomic_write_json
from coderAI.tools.filesystem import resolve_under_project

PLAN_SCHEMA_VERSION = 2


class PlanStepSpec(BaseModel):
    id: str = Field(..., min_length=1, description="Stable short ID such as step-1")
    title: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    files: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)


class PlanRiskSpec(BaseModel):
    risk: str = Field(..., min_length=1)
    mitigation: str = Field(..., min_length=1)


class PlanQuestionSpec(BaseModel):
    """One stable, directly editable planning decision."""

    id: str = Field(..., min_length=1, description="Stable short ID such as storage-backend")
    prompt: str = Field(..., min_length=1)
    choices: list[str] = Field(default_factory=list)
    answer: Optional[str] = None


class PlanProposal(BaseModel):
    summary: str = Field(..., min_length=1)
    success_criteria: list[str] = Field(..., min_length=1)
    in_scope: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    steps: list[PlanStepSpec] = Field(..., min_length=1)
    risks: list[PlanRiskSpec] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)
    rollout: list[str] = Field(default_factory=list)
    questions: list[PlanQuestionSpec] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _migrate_unanswered_questions(cls, value: Any) -> Any:
        """Accept schema-v1 proposals while emitting only structured questions."""
        if not isinstance(value, dict) or "questions" in value:
            return value
        legacy = value.get("unanswered_questions")
        if not isinstance(legacy, list):
            return value
        migrated = dict(value)
        migrated.pop("unanswered_questions", None)
        migrated["questions"] = [
            {"id": f"question-{index}", "prompt": str(prompt)}
            for index, prompt in enumerate(legacy, start=1)
        ]
        return migrated

    @property
    def pending_questions(self) -> list[PlanQuestionSpec]:
        return [question for question in self.questions if not (question.answer or "").strip()]

    @property
    def unanswered_questions(self) -> list[str]:
        """Schema-v1 compatibility view used by older callers and records."""
        return [question.prompt for question in self.pending_questions]


class PlanApprovalRecord(BaseModel):
    revision: int
    approved_at: float
    snapshot_hash: str


class PlanExecutionRecord(BaseModel):
    revision: int
    session_id: Optional[str] = None
    started_at: float
    finished_at: Optional[float] = None
    status: Literal[
        "executing", "completed", "failed", "blocked", "amendment_requested", "interrupted"
    ]
    stop_reason: Optional[str] = None


class EditablePlanDraft(BaseModel):
    """Mutable user-owned working copy; applying it creates an immutable revision."""

    schema_version: int = PLAN_SCHEMA_VERSION
    plan_id: str
    base_revision: int
    objective: str
    amendment: str = "Edited the plan artifact"
    proposal: PlanProposal


def validate_plan_proposal(proposal: PlanProposal) -> None:
    """Enforce structural invariants for model- and user-authored plans."""
    ids = [step.id for step in proposal.steps]
    if len(ids) != len(set(ids)):
        raise ValueError("Plan step IDs must be unique.")
    known = set(ids)
    invalid = {dep for step in proposal.steps for dep in step.depends_on if dep not in known}
    if invalid:
        raise ValueError("Unknown step dependencies: " + ", ".join(sorted(invalid)))
    if any(not criterion.strip() for criterion in proposal.success_criteria):
        raise ValueError("Success criteria may not be blank.")
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
        raise ValueError("Plan step dependencies contain a cycle.")
    question_ids = [question.id for question in proposal.questions]
    if len(question_ids) != len(set(question_ids)):
        raise ValueError("Plan question IDs must be unique.")
    for question in proposal.questions:
        normalized_choices = [choice.casefold() for choice in question.choices]
        if len(normalized_choices) != len(set(normalized_choices)):
            raise ValueError(f"Question {question.id} contains duplicate choices.")
        if (
            question.answer
            and question.choices
            and question.answer.casefold() not in {choice.casefold() for choice in question.choices}
        ):
            raise ValueError(
                f"Answer for {question.id} must be one of: " + ", ".join(question.choices)
            )


class PlanRecord(BaseModel):
    schema_version: int = PLAN_SCHEMA_VERSION
    plan_id: str
    project_root: str
    source_session_id: Optional[str] = None
    objective: str
    revision: int = 1
    status: Literal[
        "draft",
        "needs_input",
        "approved",
        "executing",
        "paused",
        "completed",
        "failed",
        "cancelled",
    ] = "draft"
    proposal: PlanProposal
    amendment: Optional[str] = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    approved_at: Optional[float] = None
    approved_revision: Optional[int] = None
    approved_snapshot_hash: Optional[str] = None
    execution_started_at: Optional[float] = None
    execution_finished_at: Optional[float] = None
    execution_stop_reason: Optional[str] = None
    execution_session_id: Optional[str] = None
    approvals: list[PlanApprovalRecord] = Field(default_factory=list)
    executions: list[PlanExecutionRecord] = Field(default_factory=list)


class PlanStore:
    """Persist immutable plan revisions plus mutable execution state."""

    def __init__(self, project_root: str) -> None:
        self.project_root = str(Path(project_root).resolve())
        self.root = resolve_under_project(
            Path(self.project_root) / ".coderAI" / "plans",
            operation="manage plans",
            check_protected=True,
            reject_symlink=True,
        )
        self.root.mkdir(parents=True, exist_ok=True)

    def create(
        self, objective: str, proposal: PlanProposal, *, source_session_id: Optional[str]
    ) -> PlanRecord:
        validate_plan_proposal(proposal)
        record = PlanRecord(
            plan_id=uuid.uuid4().hex,
            project_root=self.project_root,
            source_session_id=source_session_id,
            objective=objective,
            proposal=proposal,
            status="needs_input" if proposal.unanswered_questions else "draft",
        )
        self._write_revision(record)
        self._write_record(record)
        self._write_draft(record)
        self._set_active(record.plan_id)
        return record

    def revise(self, current: PlanRecord, proposal: PlanProposal, amendment: str) -> PlanRecord:
        validate_plan_proposal(proposal)
        self._require_current(current, "revised")
        if current.status in {"executing", "completed", "failed", "cancelled"}:
            raise ValueError(f"Plan cannot be revised while status is {current.status}.")
        record = current.model_copy(deep=True)
        record.revision += 1
        record.proposal = proposal
        record.amendment = amendment
        record.status = "needs_input" if proposal.unanswered_questions else "draft"
        record.updated_at = time.time()
        record.approved_at = None
        record.approved_revision = None
        record.approved_snapshot_hash = None
        self._write_revision(record)
        self._write_record(record)
        self._write_draft(record)
        self._set_active(record.plan_id)
        return record

    def answer_question(self, current: PlanRecord, question_id: str, answer: str) -> PlanRecord:
        """Create a revision containing a direct, deterministic question answer."""
        answer = answer.strip()
        if not answer:
            raise ValueError("Question answers may not be blank.")
        proposal = current.proposal.model_copy(deep=True)
        question = next((item for item in proposal.questions if item.id == question_id), None)
        if question is None:
            raise ValueError(f"Unknown plan question: {question_id}")
        if question.choices:
            matched = next(
                (choice for choice in question.choices if choice.casefold() == answer.casefold()),
                None,
            )
            if matched is None:
                raise ValueError("Answer must be one of: " + ", ".join(question.choices))
            answer = matched
        question.answer = answer
        return self.revise(current, proposal, f"Answered {question_id}: {answer}")

    def draft_path(self, record: PlanRecord) -> Path:
        return self._plan_dir(record.plan_id) / "draft.json"

    def refresh_draft(self, record: PlanRecord) -> Path:
        """Discard unapplied draft edits and recreate the working copy."""
        self._require_current(record, "refreshed")
        self._write_draft(record)
        return self.draft_path(record)

    def apply_draft(self, current: PlanRecord, path: Optional[str] = None) -> PlanRecord:
        """Validate a user-edited working copy and create an immutable revision."""
        self._require_current(current, "applied")
        draft_path = (
            self.draft_path(current)
            if path is None
            else resolve_under_project(
                path,
                operation="apply plan draft",
                check_protected=True,
                reject_symlink=True,
            )
        )
        try:
            draft_path.relative_to(Path(self.project_root))
        except ValueError:
            raise ValueError(
                f"Plan drafts must remain inside project root: {self.project_root}"
            ) from None
        if draft_path.is_symlink():
            raise ValueError("Plan draft may not be a symlink.")
        try:
            draft = EditablePlanDraft.model_validate_json(draft_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"Plan draft is invalid: {exc}") from exc
        if draft.plan_id != current.plan_id or draft.objective != current.objective:
            raise ValueError("Plan draft identity does not match the active plan.")
        if draft.base_revision != current.revision:
            raise ValueError(
                f"Plan draft is based on revision {draft.base_revision}, "
                f"but revision {current.revision} is active."
            )
        if draft.proposal == current.proposal:
            raise ValueError("Plan draft has no changes to apply.")
        return self.revise(current, draft.proposal, draft.amendment.strip() or "Edited plan draft")

    def load_active(self) -> Optional[PlanRecord]:
        pointer = self.root / "active.json"
        if not pointer.is_file() or pointer.is_symlink():
            return None
        try:
            raw = json.loads(pointer.read_text(encoding="utf-8"))
            plan_id = str(raw.get("plan_id") or "")
        except (OSError, json.JSONDecodeError, AttributeError):
            return None
        if not plan_id:
            return None
        try:
            return self.load(plan_id)
        except ValueError:
            return None

    def load(self, plan_id: str) -> Optional[PlanRecord]:
        path = self._plan_dir(plan_id) / "record.json"
        if not path.is_file() or path.is_symlink():
            return None
        try:
            return PlanRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def approve(self, record: PlanRecord) -> PlanRecord:
        self._require_current(record, "approved")
        if record.status not in {"draft", "needs_input"}:
            raise ValueError(f"Plan cannot be approved while status is {record.status}.")
        if record.proposal.unanswered_questions:
            raise ValueError("Plan still has unanswered questions; amend it before approval.")
        self._require_applied_draft(record)
        approved = record.model_copy(deep=True)
        revision_path = self._revision_path(approved.plan_id, approved.revision)
        if revision_path.is_symlink():
            raise ValueError("Plan revision may not be a symlink.")
        snapshot = revision_path.read_bytes()
        approved.status = "approved"
        approved.approved_at = time.time()
        approved.approved_revision = approved.revision
        approved.approved_snapshot_hash = hashlib.sha256(snapshot).hexdigest()
        approved.approvals.append(
            PlanApprovalRecord(
                revision=approved.revision,
                approved_at=approved.approved_at,
                snapshot_hash=approved.approved_snapshot_hash,
            )
        )
        approved.updated_at = approved.approved_at
        self._write_record(approved)
        return approved

    def mark_executing(
        self,
        record: PlanRecord,
        *,
        execution_session_id: Optional[str] = None,
        resume: bool = False,
    ) -> PlanRecord:
        self._require_current(record, "executed")
        if record.status not in ({"approved", "executing", "paused"} if resume else {"approved"}):
            raise ValueError(f"Plan cannot execute while status is {record.status}.")
        revision = record.approved_revision or record.revision
        self.load_revision(record, revision)
        executing = record.model_copy(deep=True)
        now = time.time()
        if resume and executing.executions and executing.executions[-1].status == "executing":
            executing.executions[-1].status = "interrupted"
            executing.executions[-1].finished_at = now
            executing.executions[-1].stop_reason = "session_resumed"
        executing.status = "executing"
        executing.execution_started_at = now
        executing.execution_finished_at = None
        executing.execution_stop_reason = None
        executing.execution_session_id = execution_session_id
        executing.executions.append(
            PlanExecutionRecord(
                revision=revision,
                session_id=execution_session_id,
                started_at=now,
                status="executing",
            )
        )
        executing.updated_at = now
        self._write_record(executing)
        return executing

    def mark_finished(self, record: PlanRecord, *, success: bool, stop_reason: str) -> PlanRecord:
        self._require_current(record, "finished")
        if record.status != "executing":
            raise ValueError(f"Plan cannot finish while status is {record.status}.")
        finished = record.model_copy(deep=True)
        finished.status = "completed" if success else "failed"
        finished.execution_finished_at = time.time()
        finished.execution_stop_reason = stop_reason
        finished.updated_at = finished.execution_finished_at
        if finished.executions and finished.executions[-1].status == "executing":
            finished.executions[-1].status = "completed" if success else "failed"
            finished.executions[-1].finished_at = finished.execution_finished_at
            finished.executions[-1].stop_reason = stop_reason
        self._write_record(finished)
        return finished

    def mark_paused(self, record: PlanRecord, *, stop_reason: str) -> PlanRecord:
        """Preserve approval after a denial/cancellation so execution can resume explicitly."""
        self._require_current(record, "paused")
        if record.status != "executing":
            raise ValueError(f"Plan cannot pause while status is {record.status}.")
        paused = record.model_copy(deep=True)
        paused.status = "paused"
        paused.execution_finished_at = time.time()
        paused.execution_stop_reason = stop_reason
        paused.updated_at = paused.execution_finished_at
        if paused.executions and paused.executions[-1].status == "executing":
            paused.executions[-1].status = "blocked"
            paused.executions[-1].finished_at = paused.execution_finished_at
            paused.executions[-1].stop_reason = stop_reason
        self._write_record(paused)
        return paused

    def request_execution_amendment(
        self, current: PlanRecord, proposal: PlanProposal, reason: str
    ) -> PlanRecord:
        """Stop an executing revision and persist the proposed divergence for reapproval."""
        self._require_current(current, "amended during execution")
        if current.status != "executing":
            raise ValueError("Execution amendments require an executing plan.")
        reason = reason.strip()
        if not reason:
            raise ValueError("Execution amendment reason may not be blank.")
        validate_plan_proposal(proposal)
        amended = current.model_copy(deep=True)
        now = time.time()
        if amended.executions and amended.executions[-1].status == "executing":
            amended.executions[-1].status = "amendment_requested"
            amended.executions[-1].finished_at = now
            amended.executions[-1].stop_reason = reason
        amended.execution_finished_at = now
        amended.execution_stop_reason = "amendment_requested"
        amended.revision += 1
        amended.proposal = proposal
        amended.amendment = f"Execution divergence from r{current.revision}: {reason}"
        amended.status = "needs_input" if proposal.unanswered_questions else "draft"
        amended.updated_at = now
        amended.approved_at = None
        amended.approved_revision = None
        amended.approved_snapshot_hash = None
        self._write_revision(amended)
        self._write_record(amended)
        self._write_draft(amended)
        self._set_active(amended.plan_id)
        return amended

    def cancel(self, record: PlanRecord) -> PlanRecord:
        self._require_current(record, "cancelled")
        cancelled = record.model_copy(deep=True)
        cancelled.status = "cancelled"
        cancelled.updated_at = time.time()
        self._write_record(cancelled)
        return cancelled

    def load_revision(self, record: PlanRecord, revision: int) -> PlanProposal:
        path = self._revision_path(record.plan_id, revision)
        if path.is_symlink():
            raise ValueError("Plan revision may not be a symlink.")
        snapshot = path.read_bytes()
        if record.approved_revision == revision and record.approved_snapshot_hash:
            actual_hash = hashlib.sha256(snapshot).hexdigest()
            if actual_hash != record.approved_snapshot_hash:
                raise ValueError(
                    "Approved plan revision changed after approval; refusing execution."
                )
        raw = json.loads(snapshot)
        return PlanProposal.model_validate(raw["proposal"])

    def revision_history(self, record: PlanRecord) -> list[dict[str, Any]]:
        """Return stable revision metadata without trusting directory contents."""
        history: list[dict[str, Any]] = []
        for revision in range(1, record.revision + 1):
            path = self._revision_path(record.plan_id, revision)
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"Plan revision {revision} is missing or unsafe.")
            raw = json.loads(path.read_text(encoding="utf-8"))
            history.append(
                {
                    "revision": revision,
                    "amendment": raw.get("amendment"),
                    "created_at": raw.get("created_at"),
                }
            )
        return history

    def _plan_dir(self, plan_id: str) -> Path:
        if not plan_id or any(ch not in "0123456789abcdef" for ch in plan_id.lower()):
            raise ValueError("Invalid plan ID")
        path = self.root / plan_id
        if path.is_symlink():
            raise ValueError("Plan directory may not be a symlink.")
        path.mkdir(parents=True, exist_ok=True)
        path.resolve().relative_to(self.root.resolve())
        return path

    def _revision_path(self, plan_id: str, revision: int) -> Path:
        return self._plan_dir(plan_id) / f"revision-{revision}.json"

    def _write_revision(self, record: PlanRecord) -> None:
        path = self._revision_path(record.plan_id, record.revision)
        if path.exists():
            raise ValueError(f"Plan revision already exists: {record.plan_id} r{record.revision}")
        atomic_write_json(
            path,
            {
                "schema_version": record.schema_version,
                "plan_id": record.plan_id,
                "revision": record.revision,
                "objective": record.objective,
                "proposal": record.proposal.model_dump(),
                "amendment": record.amendment,
                "created_at": time.time(),
            },
            fsync=True,
        )

    def _write_record(self, record: PlanRecord) -> None:
        atomic_write_json(self._plan_dir(record.plan_id) / "record.json", record.model_dump())

    def _write_draft(self, record: PlanRecord) -> None:
        atomic_write_json(
            self.draft_path(record),
            EditablePlanDraft(
                plan_id=record.plan_id,
                base_revision=record.revision,
                objective=record.objective,
                proposal=record.proposal,
            ).model_dump(),
        )

    def _require_applied_draft(self, record: PlanRecord) -> None:
        path = self.draft_path(record)
        if path.is_symlink():
            raise ValueError("Plan draft may not be a symlink.")
        try:
            draft = EditablePlanDraft.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"Plan draft is invalid: {exc}") from exc
        if (
            draft.plan_id != record.plan_id
            or draft.objective != record.objective
            or draft.base_revision != record.revision
        ):
            raise ValueError("Plan draft is stale or belongs to another plan; refresh it first.")
        if draft.proposal != record.proposal:
            raise ValueError("Plan draft has unapplied edits; run /plan apply before approval.")

    def _require_current(self, record: PlanRecord, action: str) -> None:
        latest = self.load(record.plan_id)
        if latest is None or latest.revision != record.revision or latest.status != record.status:
            raise ValueError(f"Plan changed before it was {action}; reload the current revision.")

    def _set_active(self, plan_id: str) -> None:
        atomic_write_json(self.root / "active.json", {"plan_id": plan_id})


def render_plan_markdown(
    record: PlanRecord, *, include_actions: bool = True, include_questions: bool = True
) -> str:
    p = record.proposal
    lines = [
        f"# Plan {record.plan_id[:8]} · revision {record.revision}",
        "",
        f"**Status:** {record.status}",
        "",
        p.summary,
        "",
        "## Success criteria",
    ]
    lines.extend(f"- {item}" for item in p.success_criteria)
    if p.in_scope:
        lines.extend(["", "## In scope", *(f"- {item}" for item in p.in_scope)])
    if p.out_of_scope:
        lines.extend(["", "## Out of scope", *(f"- {item}" for item in p.out_of_scope)])
    lines.extend(["", "## Implementation"])
    for step in p.steps:
        deps = f" (after {', '.join(step.depends_on)})" if step.depends_on else ""
        lines.append(f"1. **{step.id}: {step.title}**{deps} — {step.description}")
        if step.files:
            lines.append(f"   Files: {', '.join(step.files)}")
        if step.checks:
            lines.append(f"   Checks: {', '.join(step.checks)}")
    if p.risks:
        lines.extend(["", "## Risks"])
        lines.extend(f"- {risk.risk} — {risk.mitigation}" for risk in p.risks)
    if p.tests:
        lines.extend(["", "## Test plan", *(f"- {item}" for item in p.tests)])
    if include_questions and p.questions:
        lines.extend(["", "## Decisions"])
        for question in p.questions:
            answer = question.answer or "unanswered"
            choices = f" ({' / '.join(question.choices)})" if question.choices else ""
            lines.append(f"- **{question.id}**: {question.prompt}{choices} — {answer}")
    if include_actions and record.status in {"draft", "needs_input"}:
        lines.extend(
            [
                "",
                "Edit the validated working copy with `/plan edit`, apply it with `/plan apply`, "
                "answer decisions with `/plan answer <id> <answer>`, or use `/plan approve`.",
            ]
        )
    elif include_actions and record.status == "paused":
        lines.extend(
            [
                "",
                "Execution is paused. Use `/plan resume` to retry the approved revision or "
                "`/plan amend <instruction>` to create a revision for reapproval.",
            ]
        )
    return "\n".join(lines)


def build_execution_prompt(record: PlanRecord, proposal: PlanProposal) -> str:
    plan_text = render_plan_markdown(
        record.model_copy(update={"proposal": proposal}), include_actions=False
    )
    return (
        f"Execute approved plan {record.plan_id} revision {record.approved_revision}.\n\n"
        f"{plan_text}\n\n"
        "Implementation contract: follow the approved scope and decisions; keep the objective "
        "ledger current; inspect changed files; run every listed relevant check after the final "
        "mutation. If implementation must diverge from scope, decisions, interfaces, steps, or "
        "checks, call request_plan_amendment with the reason and full replacement plan before "
        "performing the divergent mutation. That stops execution until the new revision is approved."
    )
