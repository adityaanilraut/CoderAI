"""Versioned, project-scoped planning artifacts for real Plan Mode."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from coderAI.system.fsperms import atomic_write_json
from coderAI.tools.filesystem import resolve_under_project

PLAN_SCHEMA_VERSION = 1


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
    unanswered_questions: list[str] = Field(default_factory=list)


class PlanRecord(BaseModel):
    schema_version: int = PLAN_SCHEMA_VERSION
    plan_id: str
    project_root: str
    source_session_id: Optional[str] = None
    objective: str
    revision: int = 1
    status: Literal[
        "draft", "needs_input", "approved", "executing", "completed", "failed", "cancelled"
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
        self._set_active(record.plan_id)
        return record

    def revise(self, current: PlanRecord, proposal: PlanProposal, amendment: str) -> PlanRecord:
        latest = self.load(current.plan_id)
        if latest is None or latest.revision != current.revision or latest.status != current.status:
            raise ValueError("Plan changed while it was being revised; reload and try again.")
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
        self._set_active(record.plan_id)
        return record

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
        latest = self.load(record.plan_id)
        if latest is None or latest.revision != record.revision or latest.status != record.status:
            raise ValueError("Plan changed before approval; review the current revision.")
        if record.proposal.unanswered_questions:
            raise ValueError("Plan still has unanswered questions; amend it before approval.")
        approved = record.model_copy(deep=True)
        revision_path = self._revision_path(approved.plan_id, approved.revision)
        if revision_path.is_symlink():
            raise ValueError("Plan revision may not be a symlink.")
        snapshot = revision_path.read_bytes()
        approved.status = "approved"
        approved.approved_at = time.time()
        approved.approved_revision = approved.revision
        approved.approved_snapshot_hash = hashlib.sha256(snapshot).hexdigest()
        approved.updated_at = approved.approved_at
        self._write_record(approved)
        return approved

    def mark_executing(self, record: PlanRecord) -> PlanRecord:
        executing = record.model_copy(deep=True)
        executing.status = "executing"
        executing.execution_started_at = time.time()
        executing.updated_at = executing.execution_started_at
        self._write_record(executing)
        return executing

    def mark_finished(self, record: PlanRecord, *, success: bool, stop_reason: str) -> PlanRecord:
        finished = record.model_copy(deep=True)
        finished.status = "completed" if success else "failed"
        finished.execution_finished_at = time.time()
        finished.execution_stop_reason = stop_reason
        finished.updated_at = finished.execution_finished_at
        self._write_record(finished)
        return finished

    def cancel(self, record: PlanRecord) -> PlanRecord:
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
                "created_at": time.time(),
            },
            fsync=True,
        )

    def _write_record(self, record: PlanRecord) -> None:
        atomic_write_json(self._plan_dir(record.plan_id) / "record.json", record.model_dump())

    def _set_active(self, plan_id: str) -> None:
        atomic_write_json(self.root / "active.json", {"plan_id": plan_id})


def render_plan_markdown(record: PlanRecord, *, include_actions: bool = True) -> str:
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
    if p.unanswered_questions:
        lines.extend(["", "## Needs input", *(f"- {item}" for item in p.unanswered_questions)])
    if include_actions and record.status in {"draft", "needs_input"}:
        lines.extend(
            [
                "",
                "Use `/plan approve` to execute this exact revision, "
                "`/plan amend <instruction>` to revise it, or `/plan cancel`.",
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
        "mutation; surface any required amendment instead of silently changing the plan."
    )
