"""DeepSeek Harness goal domain: single persisted same-session completion goal.

Mirrors ``packages/goal/goal``: durable phases ``active | paused | blocked |
complete``, compare-and-set ``{id, revision}`` refs, a ``blockedReason``
``{code, message}`` pair, a positive ``maxGoalRounds`` cap (default 256), an
admitted-round counter ``roundsStarted``, and process-local ``activation``
(``armed``/``disarmed`` — never persisted, so a reloaded/resumed goal starts
disarmed and must be rearmed with ``update_goal action resume``).

This is a separate domain from the legacy ``coderai.core.goals.GoalStore``
(which keeps its action-style status vocabulary and 20-round default for
backward compatibility).
"""

from __future__ import annotations

import json
import logging
import pathlib
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from coderai.core.orchestration import get_orchestration_event_bus, resolve_goal_defaults

logger = logging.getLogger(__name__)

GOAL_PHASES = ("active", "paused", "blocked", "complete")
GOAL_ACTIVATIONS = ("armed", "disarmed")

# Machine-routable blocked codes (harness vocabulary).
BLOCK_CODE_ROUND_LIMIT = "round-limit"
BLOCK_CODE_MODEL_REPORTED = "model-reported"
BLOCK_CODE_QUEUE_FAILED = "queue-failed"
BLOCK_CODE_PROMPT_REJECTED = "prompt-rejected"


class GoalError(Exception):
    def __init__(self, message: str, code: str = "GOAL_INVALID_OPERATION") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass
class GoalRef:
    id: str
    revision: int


@dataclass
class GoalBlockReason:
    code: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message}


@dataclass
class DSHGoal:
    """Full durable state written by every non-clear goal mutation."""

    id: str
    objective: str
    phase: str = "active"
    max_goal_rounds: int = 256
    revision: int = 1
    blocked_reason: GoalBlockReason | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # Process-local continuation eligibility; never persisted.
    activation: str = "armed"

    # Highest admitted goal round number; derived from the session log in the
    # reference, stored explicitly here.
    rounds_started: int = 0

    def ref(self) -> GoalRef:
        return GoalRef(id=self.id, revision=self.revision)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "objective": self.objective,
            "phase": self.phase,
            "maxGoalRounds": self.max_goal_rounds,
            "revision": self.revision,
            "roundsStarted": self.rounds_started,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }
        if self.blocked_reason is not None:
            data["blockedReason"] = self.blocked_reason.to_dict()
        return data

    def tool_value(self) -> dict[str, Any]:
        """Canonical goal-tool output (compact harness JSON)."""
        goal: dict[str, Any] = {
            "id": self.id,
            "revision": self.revision,
            "objective": self.objective,
            "phase": self.phase,
            "roundsStarted": self.rounds_started,
            "maxGoalRounds": self.max_goal_rounds,
        }
        if self.blocked_reason is not None:
            goal["blockedReason"] = self.blocked_reason.to_dict()
        return {"goal": goal, "activation": self.activation}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DSHGoal":
        blocked = data.get("blockedReason")
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:8]),
            objective=str(data.get("objective") or ""),
            phase=str(data.get("phase") or "active")
            if str(data.get("phase") or "active") in GOAL_PHASES
            else "active",
            max_goal_rounds=int(data.get("maxGoalRounds", 256)),
            revision=int(data.get("revision", 1)),
            blocked_reason=(
                GoalBlockReason(
                    code=str(blocked.get("code") or "model-reported"),
                    message=str(blocked.get("message") or ""),
                )
                if isinstance(blocked, dict)
                else None
            ),
            created_at=float(data.get("createdAt", time.time())),
            updated_at=float(data.get("updatedAt", time.time())),
            activation="disarmed",  # process-local; reloaded goals start disarmed
            rounds_started=int(data.get("roundsStarted", 0)),
        )


class DSHGoalStore:
    """Thread-safe persisted single-goal-per-session store."""

    def __init__(self, root_dir: str | pathlib.Path = ".coderai/goals") -> None:
        self.root = pathlib.Path(root_dir)
        self._cache: dict[str, DSHGoal] = {}
        self._lock = threading.RLock()
        # Set by the goal-round driver while an automatic round is in flight,
        # gating the model's self-`blocked` threshold check.
        self._in_goal_round: set[str] = set()

    def _path(self, session_id: str) -> pathlib.Path:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in session_id) or "default"
        return self.root / f"dsh-goal-{safe}.json"

    def load(self, session_id: str) -> DSHGoal | None:
        with self._lock:
            if session_id in self._cache:
                return self._cache[session_id]
            path = self._path(session_id)
            if not path.is_file():
                return None
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                goal = DSHGoal.from_dict(raw)
            except Exception:
                return None
            self._cache[session_id] = goal
            return goal

    def _save(self, session_id: str, goal: DSHGoal) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._path(session_id).write_text(
            json.dumps(goal.to_dict(), indent=2) + "\n", encoding="utf-8"
        )
        self._cache[session_id] = goal

    # -- query / CAS ---------------------------------------------------------

    def get(self, session_id: str) -> DSHGoal | None:
        return self.load(session_id)

    def get_ref(self, session_id: str, goal_id: str, revision: Any) -> DSHGoal:
        """Resolve the CAS ref, failing loud on mismatch (harness GOAL_STALE)."""
        goal = self.load(session_id)
        if goal is None:
            raise GoalError(f"goal '{goal_id}' does not exist", "GOAL_NOT_FOUND")
        if goal.id != str(goal_id):
            raise GoalError(f"goal '{goal_id}' does not exist", "GOAL_NOT_FOUND")
        try:
            rev = int(revision)
        except (TypeError, ValueError):
            rev = -1
        if rev != goal.revision:
            raise GoalError(
                f"goal '{goal_id}' revision {revision} is stale (current {goal.revision})",
                "GOAL_STALE",
            )
        return goal

    def _mutate(self, session_id: str, goal: DSHGoal, changes: dict[str, Any]) -> DSHGoal:
        """Apply one durable mutation: bump revision, persist, publish goal/changed."""
        for key, value in changes.items():
            setattr(goal, key, value)
        goal.revision += 1
        goal.updated_at = time.time()
        self._save(session_id, goal)
        get_orchestration_event_bus().emit(
            "goal/changed",
            {
                "sessionId": session_id,
                "ref": goal.ref().__dict__,
                "goal": goal.to_dict(),
            },
        )
        return goal

    # -- operations ----------------------------------------------------------

    def create(
        self,
        session_id: str,
        objective: str,
        max_goal_rounds: int | None = None,
    ) -> DSHGoal:
        defaults = resolve_goal_defaults()
        if max_goal_rounds is None:
            max_goal_rounds = defaults["max_goal_rounds"]
        if not isinstance(max_goal_rounds, int) or max_goal_rounds < 1:
            raise GoalError(
                "maxGoalRounds must be a positive safe integer", "GOAL_INVALID_MAX_ROUNDS"
            )
        objective = (objective or "").strip()
        if not objective:
            raise GoalError("objective must be a non-empty string", "GOAL_INVALID_OBJECTIVE")
        with self._lock:
            existing = self.load(session_id)
            if existing is not None and existing.phase in ("active", "paused"):
                existing.phase = "paused"  # superseded goal is paused
                self._mutate(session_id, existing, {"phase": "paused"})
            goal = DSHGoal(
                id=uuid.uuid4().hex[:8],
                objective=objective,
                phase="active",
                max_goal_rounds=max_goal_rounds,
                revision=1,
                activation="armed",
                rounds_started=0,
            )
            self._save(session_id, goal)
            get_orchestration_event_bus().emit(
                "goal/changed",
                {"sessionId": session_id, "ref": goal.ref().__dict__, "goal": goal.to_dict()},
            )
            return goal

    def edit(
        self, session_id: str, ref: GoalRef, objective: str | None, max_goal_rounds: int | None
    ) -> DSHGoal:
        goal = self.get_ref(session_id, ref.id, ref.revision)
        if objective is None and max_goal_rounds is None:
            raise GoalError(
                "goal edit requires objective and/or maxGoalRounds", "GOAL_INVALID_EDIT"
            )
        changes: dict[str, Any] = {}
        if objective is not None:
            stripped = objective.strip()
            if not stripped:
                raise GoalError("objective must be a non-empty string", "GOAL_INVALID_OBJECTIVE")
            changes["objective"] = stripped
        if max_goal_rounds is not None:
            if not isinstance(max_goal_rounds, int) or max_goal_rounds < 1:
                raise GoalError(
                    "maxGoalRounds must be a positive safe integer", "GOAL_INVALID_MAX_ROUNDS"
                )
            changes["max_goal_rounds"] = max_goal_rounds
        with self._lock:
            return self._mutate(session_id, goal, changes)

    def pause(self, session_id: str, ref: GoalRef) -> DSHGoal:
        goal = self.get_ref(session_id, ref.id, ref.revision)
        with self._lock:
            return self._mutate(session_id, goal, {"phase": "paused"})

    def resume(self, session_id: str, ref: GoalRef) -> DSHGoal:
        goal = self.get_ref(session_id, ref.id, ref.revision)
        if goal.rounds_started >= goal.max_goal_rounds:
            raise GoalError(
                f'goal "{goal.id}" exhausted {goal.max_goal_rounds} goal rounds; '
                "increase maxGoalRounds before resuming",
                "GOAL_ROUNDS_EXHAUSTED",
            )
        with self._lock:
            return self._mutate(session_id, goal, {"phase": "active"})

    def complete(self, session_id: str, ref: GoalRef) -> DSHGoal:
        goal = self.get_ref(session_id, ref.id, ref.revision)
        with self._lock:
            return self._mutate(session_id, goal, {"phase": "complete", "blocked_reason": None})

    def block(self, session_id: str, ref: GoalRef, reason: GoalBlockReason) -> DSHGoal:
        goal = self.get_ref(session_id, ref.id, ref.revision)
        with self._lock:
            return self._mutate(session_id, goal, {"phase": "blocked", "blocked_reason": reason})

    # -- activation / rounds ---------------------------------------------------

    def disarm(self, session_id: str) -> None:
        goal = self.load(session_id)
        if goal is not None:
            goal.activation = "disarmed"

    def arm(self, session_id: str) -> None:
        goal = self.load(session_id)
        if goal is not None:
            goal.activation = "armed"

    def admit_round(self, session_id: str) -> int | None:
        """Admit one automatic round: increment roundsStarted, return the round number."""
        goal = self.load(session_id)
        if goal is None:
            return None
        with self._lock:
            goal.rounds_started += 1
            goal.updated_at = time.time()
            self._save(session_id, goal)
            return goal.rounds_started

    def in_goal_round(self, session_id: str) -> bool:
        return session_id in self._in_goal_round

    def set_in_goal_round(self, session_id: str, value: bool) -> None:
        if value:
            self._in_goal_round.add(session_id)
        else:
            self._in_goal_round.discard(session_id)


_global_dsh_goal_store: DSHGoalStore | None = None


def get_dsh_goal_store(project_root: str = ".") -> DSHGoalStore:
    global _global_dsh_goal_store
    if _global_dsh_goal_store is None:
        _global_dsh_goal_store = DSHGoalStore(
            root_dir=pathlib.Path(project_root) / ".coderai" / "goals"
        )
    return _global_dsh_goal_store


def reset_dsh_goal_store() -> None:
    """Test helper: drop the singleton."""
    global _global_dsh_goal_store
    _global_dsh_goal_store = None
