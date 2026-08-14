"""Structured objective evidence and deterministic completion decisions.

The model may propose that a turn is finished by returning a response without
tool calls.  This module records what actually happened during the turn so the
runtime can distinguish a plausible final message from a verified outcome.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable
from os.path import normpath
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Optional, cast

from coderAI.tools.semantics import EvidenceKind, TOOL_SEMANTICS, semantics_for


CompletionStatus = Literal["reasoned", "verified", "incomplete", "unverified"]
CriterionStatus = Literal["unverified", "verified", "reasoned", "blocked"]
_COMPLETION_STATUSES = frozenset({"reasoned", "verified", "incomplete", "unverified"})
_CRITERION_STATUSES = frozenset({"unverified", "verified", "reasoned", "blocked"})
_EVIDENCE_KINDS = frozenset({"read", "mutation", "verification", "internal"})

# Compatibility exports for callers/tests that inspected the old constants.
# Their contents are now derived from the single typed semantics catalog.
_WORKSPACE_MUTATION_TOOLS = frozenset(
    row.name for row in TOOL_SEMANTICS if row.workspace_mutation
)
_REQUIRES_POST_EDIT_INSPECTION = frozenset(
    row.name for row in TOOL_SEMANTICS if row.inspect_after_mutation
)
_INTERNAL_STATE_TOOLS = frozenset(
    row.name for row in TOOL_SEMANTICS if row.evidence_kind == "internal"
)
_PATH_ARGUMENTS = ("path", "file_path", "target", "destination", "dest")
_CHECK_COMMAND = re.compile(
    r"(?:^|[;&|]\s*)(?:"
    r"pytest|python\s+-m\s+(?:pytest|unittest)|"
    r"ruff\s+(?:check|format\s+--check)|mypy|pyright|tsc|"
    r"npm\s+(?:test|run\s+(?:test|lint|build|check))|"
    r"pnpm\s+(?:test|run\s+(?:test|lint|build|check))|"
    r"yarn\s+(?:test|lint|build)|cargo\s+(?:test|check|clippy)|"
    r"go\s+(?:test|vet)|golangci-lint|shellcheck|biome"
    r")\b",
    re.IGNORECASE,
)


def _string_int_dict(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): int(item)
        for key, item in value.items()
        if isinstance(item, int) and not isinstance(item, bool)
    }


def _string_bool_dict(value: Any) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(item, bool)}


@dataclass
class AcceptanceCriterion:
    description: str
    status: CriterionStatus = "unverified"
    evidence: list[str] = field(default_factory=list)


@dataclass
class PlanStep:
    task_id: int
    title: str
    status: str


@dataclass
class ToolEvidence:
    sequence: int
    tool_name: str
    success: bool
    kind: EvidenceKind
    summary: str
    artifacts: list[str] = field(default_factory=list)


@dataclass
class CompletionDecision:
    allowed: bool
    status: CompletionStatus
    issues: list[str] = field(default_factory=list)


@dataclass
class ObjectiveState:
    """Turn-local engineering state used by the completion gate."""

    objective: str
    objective_id: str = field(default_factory=lambda: f"objective_{uuid.uuid4().hex}")
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    plan_id: Optional[str] = None
    plan_revision: Optional[int] = None
    acceptance_criteria: list[AcceptanceCriterion] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    planned_steps: dict[int, PlanStep] = field(default_factory=dict)
    artifacts_changed: list[str] = field(default_factory=list)
    evidence: list[ToolEvidence] = field(default_factory=list)
    checks_required: list[str] = field(default_factory=list)
    checks_completed: list[str] = field(default_factory=list)
    unresolved_risks: list[str] = field(default_factory=list)
    completion_status: CompletionStatus = "incomplete"
    completion_gate_attempts: int = 0
    _sequence: int = 0
    _last_mutation: int = 0
    _last_verification: int = 0
    _artifact_mutations: dict[str, int] = field(default_factory=dict)
    _artifact_inspections: dict[str, int] = field(default_factory=dict)
    _last_tool_outcome: dict[str, bool] = field(default_factory=dict)
    _persist_callback: Optional[Callable[["ObjectiveState"], None]] = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not self.acceptance_criteria:
            self.acceptance_criteria.append(AcceptanceCriterion(self.objective))

    def bind_persistence(
        self,
        callback: Callable[["ObjectiveState"], None],
        *,
        persist_now: bool = True,
    ) -> None:
        """Persist now and after every subsequent ledger state transition."""
        self._persist_callback = callback
        if persist_now:
            self.persist()

    def persist(self) -> None:
        """Write the current state through the session-owned durable store."""
        self.updated_at = time.time()
        if self._persist_callback is not None:
            self._persist_callback(self)

    def record_tool_result(
        self,
        tool_name: str,
        arguments: Optional[dict[str, Any]],
        result: Any,
        tool: Any = None,
    ) -> None:
        """Normalize one real tool outcome into objective evidence."""
        self._sequence += 1
        args = arguments if isinstance(arguments, dict) else {}
        success = isinstance(result, dict) and result.get("success") is True
        artifacts = self._extract_artifacts(args, result)
        check = self._verification_label(tool_name, args)
        if success and check and isinstance(result, dict):
            if tool_name == "run_tests" and ("returncode" in result or "results" in result):
                results = result.get("results")
                success = (
                    result.get("returncode") == 0
                    and isinstance(results, dict)
                    and results.get("passed_clean") is True
                )
            elif tool_name == "lint" and "has_issues" in result:
                success = result.get("returncode") == 0 and result.get("has_issues") is False
            elif tool_name == "format" and "needs_formatting" in result:
                success = result.get("needs_formatting") is False
        observed_changes = bool(isinstance(result, dict) and result.get("_workspace_changes"))
        mutation = observed_changes or self._is_workspace_mutation(tool_name, args, tool)

        semantics = semantics_for(tool_name)
        if tool_name == "manage_tasks":
            kind: Literal["read", "mutation", "verification", "internal"] = "internal"
            self._record_task_state(args, result)
        elif check:
            kind = "verification"
            if success:
                self._last_verification = self._sequence
                self.checks_completed.append(check)
            if observed_changes:
                self._record_mutation_evidence(tool_name, artifacts)
        elif mutation:
            kind = "mutation"
            if success or observed_changes:
                self._record_mutation_evidence(tool_name, artifacts)
        else:
            kind = semantics.evidence_kind
            if success and semantics.records_inspection:
                for artifact in artifacts:
                    self._artifact_inspections[artifact] = self._sequence

        self._last_tool_outcome[tool_name] = success
        summary = self._result_summary(result)
        self.evidence.append(
            ToolEvidence(
                sequence=self._sequence,
                tool_name=tool_name,
                success=success,
                kind=kind,
                summary=summary,
                artifacts=artifacts,
            )
        )
        self.persist()

    def evaluate_completion(self) -> CompletionDecision:
        """Return the runtime's deterministic decision for a completion proposal."""
        issues: list[str] = []
        failed = sorted(name for name, ok in self._last_tool_outcome.items() if not ok)
        if failed:
            issues.append("Unresolved failed tool calls: " + ", ".join(failed) + ".")

        if self._last_mutation == 0:
            if issues:
                self.completion_status = "incomplete"
                self.unresolved_risks = list(issues)
                self.persist()
                return CompletionDecision(False, "incomplete", issues)
            self.completion_status = "reasoned"
            self.acceptance_criteria[0].status = "reasoned"
            self.persist()
            return CompletionDecision(True, "reasoned")

        pending = [
            f"#{step.task_id} {step.title} ({step.status})"
            for step in self.planned_steps.values()
            if step.status != "completed"
        ]
        if pending:
            issues.append("Task checklist still has open work: " + "; ".join(pending) + ".")

        if self._last_verification < self._last_mutation:
            issues.append(
                "No successful verification was recorded after the last workspace mutation."
            )

        uninspected = sorted(
            artifact
            for artifact, changed_at in self._artifact_mutations.items()
            if self._artifact_inspections.get(artifact, 0) <= changed_at
        )
        if uninspected:
            issues.append(
                "Changed files were not inspected after modification: "
                + ", ".join(uninspected)
                + "."
            )

        if issues:
            self.completion_status = "incomplete"
            self.unresolved_risks = list(issues)
            self.persist()
            return CompletionDecision(False, "incomplete", issues)

        self.completion_status = "verified"
        self.unresolved_risks.clear()
        criterion = self.acceptance_criteria[0]
        criterion.status = "verified"
        criterion.evidence = list(self.checks_completed)
        self.persist()
        return CompletionDecision(True, "verified")

    def mark_unverified(self, issues: list[str]) -> None:
        self.completion_status = "unverified"
        self.unresolved_risks = list(issues)
        self.acceptance_criteria[0].status = "blocked"
        self.persist()

    def as_dict(self) -> dict[str, Any]:
        """Return the stable, public portion of the objective ledger."""
        return {
            "objective_id": self.objective_id,
            "objective": self.objective,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "plan_id": self.plan_id,
            "plan_revision": self.plan_revision,
            "acceptance_criteria": [asdict(item) for item in self.acceptance_criteria],
            "constraints": list(self.constraints),
            "assumptions": list(self.assumptions),
            "planned_steps": [asdict(item) for item in self.planned_steps.values()],
            "artifacts_changed": list(self.artifacts_changed),
            "evidence": [asdict(item) for item in self.evidence],
            "checks_required": list(self.checks_required),
            "checks_completed": list(self.checks_completed),
            "unresolved_risks": list(self.unresolved_risks),
            "completion_status": self.completion_status,
        }

    def snapshot(self) -> dict[str, Any]:
        """Return all state needed to resume deterministic completion checks."""
        return {
            **self.as_dict(),
            "completion_gate_attempts": self.completion_gate_attempts,
            "sequence": self._sequence,
            "last_mutation": self._last_mutation,
            "last_verification": self._last_verification,
            "artifact_mutations": dict(self._artifact_mutations),
            "artifact_inspections": dict(self._artifact_inspections),
            "last_tool_outcome": dict(self._last_tool_outcome),
        }

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> "ObjectiveState":
        """Restore a trusted, validated snapshot from the objective store."""
        objective = data.get("objective")
        objective_id = data.get("objective_id")
        if not isinstance(objective, str) or not objective:
            raise ValueError("objective snapshot has no objective")
        if not isinstance(objective_id, str) or not objective_id.startswith("objective_"):
            raise ValueError("objective snapshot has an invalid objective_id")

        criteria = [
            AcceptanceCriterion(
                description=str(item.get("description") or ""),
                status=cast(
                    CriterionStatus,
                    item.get("status")
                    if item.get("status") in _CRITERION_STATUSES
                    else "unverified",
                ),
                evidence=[str(value) for value in item.get("evidence", [])],
            )
            for item in data.get("acceptance_criteria", [])
            if isinstance(item, dict)
        ]
        planned_steps = {
            int(item["task_id"]): PlanStep(
                task_id=int(item["task_id"]),
                title=str(item.get("title") or "task"),
                status=str(item.get("status") or "pending"),
            )
            for item in data.get("planned_steps", [])
            if isinstance(item, dict)
            and isinstance(item.get("task_id"), int)
            and not isinstance(item.get("task_id"), bool)
        }
        evidence = [
            ToolEvidence(
                sequence=int(item.get("sequence", 0)),
                tool_name=str(item.get("tool_name") or "unknown"),
                success=bool(item.get("success")),
                kind=cast(
                    EvidenceKind,
                    item.get("kind") if item.get("kind") in _EVIDENCE_KINDS else "read",
                ),
                summary=str(item.get("summary") or ""),
                artifacts=[str(value) for value in item.get("artifacts", [])],
            )
            for item in data.get("evidence", [])
            if isinstance(item, dict)
        ]
        state = cls(
            objective=objective,
            objective_id=objective_id,
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
            plan_id=data.get("plan_id") if isinstance(data.get("plan_id"), str) else None,
            plan_revision=(
                data.get("plan_revision") if isinstance(data.get("plan_revision"), int) else None
            ),
            acceptance_criteria=criteria,
            constraints=[str(value) for value in data.get("constraints", [])],
            assumptions=[str(value) for value in data.get("assumptions", [])],
            planned_steps=planned_steps,
            artifacts_changed=[str(value) for value in data.get("artifacts_changed", [])],
            evidence=evidence,
            checks_required=[str(value) for value in data.get("checks_required", [])],
            checks_completed=[str(value) for value in data.get("checks_completed", [])],
            unresolved_risks=[str(value) for value in data.get("unresolved_risks", [])],
            completion_status=cast(
                CompletionStatus,
                data.get("completion_status")
                if data.get("completion_status") in _COMPLETION_STATUSES
                else "incomplete",
            ),
            completion_gate_attempts=int(data.get("completion_gate_attempts", 0)),
        )
        state._sequence = int(data.get("sequence", 0))
        state._last_mutation = int(data.get("last_mutation", 0))
        state._last_verification = int(data.get("last_verification", 0))
        state._artifact_mutations = _string_int_dict(data.get("artifact_mutations"))
        state._artifact_inspections = _string_int_dict(data.get("artifact_inspections"))
        state._last_tool_outcome = _string_bool_dict(data.get("last_tool_outcome"))
        return state

    def _record_mutation_evidence(self, tool_name: str, artifacts: list[str]) -> None:
        self._last_mutation = self._sequence
        if "post-mutation verification" not in self.checks_required:
            self.checks_required.append("post-mutation verification")
        for artifact in artifacts:
            if artifact not in self.artifacts_changed:
                self.artifacts_changed.append(artifact)
            if semantics_for(tool_name).inspect_after_mutation or tool_name == "run_command":
                self._artifact_mutations[artifact] = self._sequence

    @staticmethod
    def _extract_artifacts(arguments: dict[str, Any], result: Any = None) -> list[str]:
        if isinstance(result, dict) and result.get("_workspace_changes"):
            return list(
                dict.fromkeys(
                    normpath(value)
                    for change in result["_workspace_changes"]
                    if isinstance(change, dict)
                    and isinstance((value := change.get("path")), str)
                    and value
                )
            )

        artifacts: list[str] = []
        for key in _PATH_ARGUMENTS:
            value = arguments.get(key)
            if isinstance(value, str) and value and value not in artifacts:
                normalized = normpath(value)
                if normalized not in artifacts:
                    artifacts.append(normalized)
        return artifacts

    @staticmethod
    def _verification_label(tool_name: str, arguments: dict[str, Any]) -> Optional[str]:
        verification = semantics_for(tool_name).verification
        if verification == "tests":
            return "tests"
        if verification == "lint" and not bool(arguments.get("fix")):
            return "lint"
        if verification == "format" and bool(arguments.get("check")):
            return "format check"
        if verification == "command":
            command = arguments.get("command")
            if isinstance(command, str) and _CHECK_COMMAND.search(command):
                return f"command: {command[:160]}"
        return None

    @staticmethod
    def _is_workspace_mutation(tool_name: str, arguments: dict[str, Any], tool: Any) -> bool:
        semantics = semantics_for(tool_name)
        if semantics.evidence_kind in {"internal", "verification"}:
            return False
        if semantics.verification == "lint":
            return bool(arguments.get("fix"))
        if semantics.verification == "format":
            return not bool(arguments.get("check"))
        if semantics.workspace_mutation:
            return True
        if semantics.verification == "command":
            return ObjectiveState._verification_label(tool_name, arguments) is None
        if tool_name == "delegate_task":
            return not bool(arguments.get("read_only_task"))
        category = getattr(tool, "category", None)
        is_read_only = getattr(tool, "is_read_only", True)
        return category in {"filesystem", "git", "terminal", "code_quality"} and not bool(
            is_read_only
        )

    @staticmethod
    def _result_summary(result: Any) -> str:
        if not isinstance(result, dict):
            return str(result)[:240]
        for key in ("message", "summary", "error", "output", "result"):
            value = result.get(key)
            if value:
                return str(value)[:240]
        return "succeeded" if result.get("success") is True else "failed"

    def _record_task_state(self, arguments: dict[str, Any], result: Any) -> None:
        if not (isinstance(result, dict) and result.get("success") is True):
            return
        action = arguments.get("action")
        task = result.get("task") if isinstance(result.get("task"), dict) else None
        task_id = arguments.get("task_id")
        if task is not None:
            task_id = task.get("id", task_id)
        if isinstance(task_id, bool) or not isinstance(task_id, int):
            return
        existing = self.planned_steps.get(task_id)
        title = str((task or {}).get("title") or arguments.get("title") or "task")
        status = str((task or {}).get("status") or (existing.status if existing else "pending"))
        if action == "start":
            status = "in_progress"
        elif action == "complete":
            status = "completed"
        elif action == "delete":
            self.planned_steps.pop(task_id, None)
            return
        self.planned_steps[task_id] = PlanStep(task_id, title, status)
