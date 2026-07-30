"""Structured objective evidence and deterministic completion decisions.

The model may propose that a turn is finished by returning a response without
tool calls.  This module records what actually happened during the turn so the
runtime can distinguish a plausible final message from a verified outcome.
"""

from __future__ import annotations

import re
from os.path import normpath
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Optional


CompletionStatus = Literal["reasoned", "verified", "incomplete", "unverified"]

_WORKSPACE_MUTATION_TOOLS = frozenset(
    {
        "apply_diff",
        "copy_file",
        "create_directory",
        "delete_file",
        "file_chmod",
        "move_file",
        "package_manager",
        "refactor",
        "search_replace",
        "write_file",
    }
)
_REQUIRES_POST_EDIT_INSPECTION = frozenset(
    {"apply_diff", "copy_file", "move_file", "refactor", "search_replace", "write_file"}
)
_INTERNAL_STATE_TOOLS = frozenset(
    {
        "manage_tasks",
        "memory_delete",
        "memory_save",
        "memory_update",
    }
)
_PATH_ARGUMENTS = ("path", "file_path", "target", "destination", "dest")
_CHECK_COMMAND = re.compile(
    r"(?:^|[;&|]\s*)(?:"
    r"pytest|python\s+-m\s+(?:pytest|unittest)|"
    r"ruff\s+(?:check|format\s+--check)|mypy|pyright|"
    r"npm\s+(?:test|run\s+(?:test|lint|build|check))|"
    r"pnpm\s+(?:test|run\s+(?:test|lint|build|check))|"
    r"yarn\s+(?:test|lint|build)|cargo\s+(?:test|check)|go\s+test"
    r")\b",
    re.IGNORECASE,
)


@dataclass
class AcceptanceCriterion:
    description: str
    status: Literal["unverified", "verified", "reasoned", "blocked"] = "unverified"
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
    kind: Literal["read", "mutation", "verification", "internal"]
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

    def __post_init__(self) -> None:
        if not self.acceptance_criteria:
            self.acceptance_criteria.append(AcceptanceCriterion(self.objective))

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
        observed_changes = bool(isinstance(result, dict) and result.get("_workspace_changes"))
        mutation = observed_changes or self._is_workspace_mutation(tool_name, args, tool)

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
            kind = "internal" if tool_name in _INTERNAL_STATE_TOOLS else "read"
            if success and tool_name == "read_file":
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
                return CompletionDecision(False, "incomplete", issues)
            self.completion_status = "reasoned"
            self.acceptance_criteria[0].status = "reasoned"
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
            return CompletionDecision(False, "incomplete", issues)

        self.completion_status = "verified"
        self.unresolved_risks.clear()
        criterion = self.acceptance_criteria[0]
        criterion.status = "verified"
        criterion.evidence = list(self.checks_completed)
        return CompletionDecision(True, "verified")

    def mark_unverified(self, issues: list[str]) -> None:
        self.completion_status = "unverified"
        self.unresolved_risks = list(issues)
        self.acceptance_criteria[0].status = "blocked"

    def as_dict(self) -> dict[str, Any]:
        """Return the stable, public portion of the objective ledger."""
        return {
            "objective": self.objective,
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

    def _record_mutation_evidence(self, tool_name: str, artifacts: list[str]) -> None:
        self._last_mutation = self._sequence
        if "post-mutation verification" not in self.checks_required:
            self.checks_required.append("post-mutation verification")
        for artifact in artifacts:
            if artifact not in self.artifacts_changed:
                self.artifacts_changed.append(artifact)
            if tool_name in _REQUIRES_POST_EDIT_INSPECTION or tool_name == "run_command":
                self._artifact_mutations[artifact] = self._sequence

    @staticmethod
    def _extract_artifacts(arguments: dict[str, Any], result: Any = None) -> list[str]:
        artifacts: list[str] = []
        for key in _PATH_ARGUMENTS:
            value = arguments.get(key)
            if isinstance(value, str) and value and value not in artifacts:
                normalized = normpath(value)
                if normalized not in artifacts:
                    artifacts.append(normalized)
        if isinstance(result, dict):
            for change in result.get("_workspace_changes", []) or []:
                if not isinstance(change, dict):
                    continue
                value = change.get("path")
                if isinstance(value, str) and value:
                    normalized = normpath(value)
                    if normalized not in artifacts:
                        artifacts.append(normalized)
        return artifacts

    @staticmethod
    def _verification_label(tool_name: str, arguments: dict[str, Any]) -> Optional[str]:
        if tool_name == "run_tests":
            return "tests"
        if tool_name == "lint" and not bool(arguments.get("fix")):
            return "lint"
        if tool_name == "format" and bool(arguments.get("check")):
            return "format check"
        if tool_name == "run_command":
            command = arguments.get("command")
            if isinstance(command, str) and _CHECK_COMMAND.search(command):
                return f"command: {command[:160]}"
        return None

    @staticmethod
    def _is_workspace_mutation(tool_name: str, arguments: dict[str, Any], tool: Any) -> bool:
        if tool_name in _INTERNAL_STATE_TOOLS or tool_name == "run_tests":
            return False
        if tool_name == "lint":
            return bool(arguments.get("fix"))
        if tool_name == "format":
            return not bool(arguments.get("check"))
        if tool_name in _WORKSPACE_MUTATION_TOOLS:
            return True
        if tool_name == "run_command":
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
