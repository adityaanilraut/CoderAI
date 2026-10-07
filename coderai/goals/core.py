"""Validated session goals with transactional JSON persistence."""

from __future__ import annotations

import builtins
import contextlib
import json
import logging
import math
import os
import pathlib
import re
import sys
import time
import uuid
from dataclasses import asdict, dataclass, replace
from typing import Any
from collections.abc import Iterator

from coderai.tools.legacy.types import ToolResult
from coderai.utils.io import atomic_json_write

logger = logging.getLogger(__name__)
DEFAULT_MAX_GOAL_ROUNDS = 20
VALID_GOAL_STATUS = ("running", "paused", "completed", "failed", "pending", "cancelled")
GOAL_ACTIONS = ("create", "status", "update", "start", "pause", "complete", "cancel")


class GoalStorageError(ValueError):
    """Goal data could not be read safely; mutations must not replace it."""


def _positive_int(value: Any, name: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer >= {minimum}.")
    try:
        parsed = int(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{name} must be an integer >= {minimum}.") from exc
    if parsed < minimum or (isinstance(value, float) and value != parsed):
        raise ValueError(f"{name} must be an integer >= {minimum}.")
    return parsed


def get_default_max_goal_rounds() -> int:
    try:
        return _positive_int(
            os.environ.get("CODERAI_GOAL_MAX_ROUNDS", DEFAULT_MAX_GOAL_ROUNDS), "max_rounds"
        )
    except ValueError:
        return DEFAULT_MAX_GOAL_ROUNDS


def _text(value: Any, name: str, *, required: bool = False) -> str:
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f"{name} must be {'a non-empty' if required else 'a'} string.")
    return value.strip()


def _milestones(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("milestones must be a list of non-empty strings.")
    return tuple(_text(item, "milestone", required=True) for item in value)


@dataclass(frozen=True)
class Goal:
    id: str
    objective: str
    status: str = "running"
    # Number of completed goal attempts; version 1 stored the next round instead.
    round: int = 0
    max_rounds: int = DEFAULT_MAX_GOAL_ROUNDS
    revision: int = 1
    description: str = ""
    milestones: tuple[str, ...] = ()
    completed_milestones: int = 0
    notes: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["milestones"] = list(self.milestones)
        return {"version": 2, **data}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Goal:
        version = _positive_int(data.get("version", 1), "version")
        if version not in (1, 2):
            raise ValueError(f"Unsupported goal version: {version}.")
        goal_id = _text(data.get("id"), "id", required=True)
        status = data.get("status", "running")
        if version == 1:
            status = {"done": "completed", "in_progress": "running"}.get(status, status)
        if status not in VALID_GOAL_STATUS:
            raise ValueError(f"Invalid goal status: {status!r}.")
        rounds = _positive_int(data.get("round", 0 if version == 2 else 1), "round", minimum=0)
        if version == 1:
            rounds = max(0, rounds - 1)
        milestones = _milestones(data.get("milestones", []))
        completed = _positive_int(
            data.get("completed_milestones", 0), "completed_milestones", minimum=0
        )
        if completed > len(milestones):
            raise ValueError("completed_milestones exceeds the milestone count.")
        created = float(data.get("created_at", time.time()))
        updated = float(data.get("updated_at", created))
        if not math.isfinite(created) or not math.isfinite(updated):
            raise ValueError("Goal timestamps must be finite.")
        return cls(
            id=goal_id,
            objective=_text(data.get("objective", data.get("title")), "objective", required=True),
            status=status,
            round=rounds,
            max_rounds=_positive_int(data.get("max_rounds", DEFAULT_MAX_GOAL_ROUNDS), "max_rounds"),
            revision=_positive_int(data.get("revision", 1), "revision"),
            description=_text(data.get("description", ""), "description"),
            milestones=milestones,
            completed_milestones=completed,
            notes=_text(data.get("notes", ""), "notes"),
            created_at=created,
            updated_at=updated,
        )


class GoalStore:
    """Reload under an OS file lock for every transaction; never expose a cache."""

    def __init__(self, root_dir: str | pathlib.Path = ".coderai/goals") -> None:
        self.root = pathlib.Path(root_dir)

    def _path(self, session_id: str) -> pathlib.Path:
        if (
            not isinstance(session_id, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id) is None
        ):
            raise ValueError("A valid session identity is required for goals.")
        return self.root / f"goals-{session_id}.json"

    @contextlib.contextmanager
    def _file_lock(self, path: pathlib.Path, *, wait: bool = True) -> Iterator[None]:
        """Use OS locks, which are released even if the owning process crashes."""
        self.root.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as stream:
            if sys.platform == "win32":
                import msvcrt

                if stream.tell() == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
            else:
                import fcntl

            deadline = time.monotonic() + 10
            while True:
                try:
                    if sys.platform == "win32":
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if not wait:
                        raise
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Timed out waiting for goal lock at {path}.")
                    time.sleep(0.01)
            try:
                yield
            finally:
                if sys.platform == "win32":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @contextlib.contextmanager
    def execution_lock(self, session_id: str) -> Iterator[None]:
        """Do not allow two processes to drive goals for the same session."""
        with self._file_lock(self._path(session_id).with_suffix(".run.lock"), wait=False):
            yield

    def _load(self, session_id: str, *, strict: bool = False) -> builtins.list[Goal]:
        path = self._path(session_id)
        if path.with_suffix(".deleted").exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (ValueError, UnicodeError) as exc:
            raise GoalStorageError(
                f"Cannot read goals at {path}: {exc}. Original file preserved."
            ) from exc
        if not isinstance(raw, list):
            raise GoalStorageError(f"Expected a goal list at {path}; original file preserved.")
        goals: builtins.list[Goal] = []
        errors: builtins.list[str] = []
        ids: set[str] = set()
        for index, item in enumerate(raw):
            try:
                if not isinstance(item, dict):
                    raise ValueError("record must be an object")
                goal = Goal.from_dict(item)
                if goal.id in ids:
                    raise ValueError("duplicate goal identity")
                ids.add(goal.id)
                goals.append(goal)
            except (ValueError, TypeError, OverflowError) as exc:
                errors.append(f"record {index + 1}: {exc}")
        if sum(g.status == "running" for g in goals) > 1:
            errors.append("multiple running goals")
        if errors:
            message = f"Invalid goals at {path}: {'; '.join(errors)}. Original file preserved; repair it before changing goals."
            if strict:
                raise GoalStorageError(message)
            logger.warning(message)
        return goals

    @contextlib.contextmanager
    def _transaction(self, session_id: str) -> Iterator[builtins.list[Goal]]:
        path = self._path(session_id)
        with self._file_lock(path.with_suffix(".lock")):
            if path.with_suffix(".deleted").exists():
                raise GoalStorageError("Session was deleted; goal changes are no longer allowed.")
            yield self._load(session_id, strict=True)

    def _save(self, session_id: str, goals: builtins.list[Goal]) -> None:
        atomic_json_write([g.to_dict() for g in goals], self._path(session_id))

    def list(self, session_id: str) -> builtins.list[Goal]:
        return self._load(session_id)

    def snapshot(self, session_id: str) -> builtins.list[dict[str, Any]]:
        return [goal.to_dict() for goal in self.list(session_id)]

    def restore(
        self, session_id: str, snapshot: builtins.list[dict[str, Any]], *, pause: bool = False
    ) -> None:
        goals = [Goal.from_dict(item) for item in snapshot]
        if (
            len({g.id for g in goals}) != len(goals)
            or sum(g.status == "running" for g in goals) > 1
        ):
            raise ValueError("Invalid goal snapshot.")
        if pause:
            goals = [
                replace(g, status="paused", revision=g.revision + 1) if g.status == "running" else g
                for g in goals
            ]
        with self._transaction(session_id):
            if goals:
                self._save(session_id, goals)
            else:
                self._path(session_id).unlink(missing_ok=True)

    def delete_session(self, session_id: str) -> None:
        path = self._path(session_id)
        with self._file_lock(path.with_suffix(".lock")):
            # Prevent a late worker from recreating data after session deletion.
            path.with_suffix(".deleted").touch()
            path.unlink(missing_ok=True)

    def get(self, session_id: str, goal_id: str) -> Goal | None:
        return next((g for g in self.list(session_id) if g.id == goal_id), None)

    def get_active_goal(self, session_id: str) -> Goal | None:
        return next((g for g in reversed(self.list(session_id)) if g.status == "running"), None)

    def create(
        self,
        session_id: str,
        objective: str,
        max_rounds: int | None = None,
        notes: str = "",
        *,
        description: str = "",
        milestones: builtins.list[str] | tuple[str, ...] = (),
        status: str = "running",
    ) -> Goal:
        if status not in VALID_GOAL_STATUS:
            raise ValueError(f"Invalid goal status: {status!r}.")
        now = time.time()
        goal = Goal.from_dict(
            {
                "version": 2,
                "id": uuid.uuid4().hex[:8],
                "objective": objective,
                "max_rounds": get_default_max_goal_rounds() if max_rounds is None else max_rounds,
                "notes": notes,
                "description": description,
                "milestones": milestones,
                "status": status,
                "created_at": now,
                "updated_at": now,
            }
        )
        with self._transaction(session_id) as goals:
            while any(g.id == goal.id for g in goals):
                goal = replace(goal, id=uuid.uuid4().hex[:8])
            if status == "running":
                goals = self._pause_others(goals, goal.id)
            self._save(session_id, [*goals, goal])
        return goal

    @staticmethod
    def _pause_others(goals: builtins.list[Goal], goal_id: str) -> builtins.list[Goal]:
        return [
            replace(g, status="paused", revision=g.revision + 1, updated_at=time.time())
            if g.id != goal_id and g.status == "running"
            else g
            for g in goals
        ]

    def update(
        self, session_id: str, goal_id: str, *, expected_revision: int | None = None, **changes: Any
    ) -> Goal | None:
        allowed = {
            "objective",
            "description",
            "status",
            "max_rounds",
            "notes",
            "milestones",
            "completed_milestones",
        }
        if changes.keys() - allowed:
            raise ValueError(f"Unknown goal fields: {', '.join(sorted(changes.keys() - allowed))}.")
        if not changes:
            raise ValueError("No goal changes supplied.")
        if "status" in changes and changes["status"] not in VALID_GOAL_STATUS:
            raise ValueError(f"Invalid goal status: {changes['status']!r}.")
        with self._transaction(session_id) as goals:
            goal = next((g for g in goals if g.id == goal_id), None)
            if goal is None:
                return None
            if expected_revision is not None and expected_revision != goal.revision:
                raise ValueError("Goal revision changed; read the latest goal before updating.")
            updated = Goal.from_dict(
                {
                    **goal.to_dict(),
                    **changes,
                    "revision": goal.revision + 1,
                    "updated_at": time.time(),
                }
            )
            if updated.status == "running" and updated.round >= updated.max_rounds:
                raise ValueError(
                    "Goal round budget is exhausted; increase max_rounds before starting."
                )
            if updated.status == "running":
                goals = self._pause_others(goals, goal_id)
            self._save(session_id, [updated if g.id == goal_id else g for g in goals])
            return updated

    def record_attempt(self, session_id: str, goal_id: str) -> Goal | None:
        """Charge a completed goal attempt; errors and suspensions are not attempts."""
        with self._transaction(session_id) as goals:
            goal = next((g for g in goals if g.id == goal_id), None)
            if goal is None or goal.status not in ("running", "completed"):
                return goal
            rounds = goal.round + 1
            exhausted = goal.status == "running" and rounds >= goal.max_rounds
            updated = replace(
                goal,
                round=rounds,
                revision=goal.revision + 1,
                updated_at=time.time(),
                status="failed" if exhausted else goal.status,
                notes=(goal.notes + "\nGoal round budget exhausted.").strip()
                if exhausted
                else goal.notes,
            )
            self._save(session_id, [updated if g.id == goal_id else g for g in goals])
            return updated

    def format_summary(self, session_id: str) -> str:
        goals = self.list(session_id)
        if not goals:
            return "No goals in current session."
        lines = ["Session goals:"]
        for goal in goals:
            lines.append(
                f"[{goal.id}] {goal.objective} | {goal.status.upper()} ({goal.round}/{goal.max_rounds} rounds completed)"
            )
            if goal.description:
                lines.append(f"  {goal.description}")
            for index, milestone in enumerate(goal.milestones):
                lines.append(f"  [{'x' if index < goal.completed_milestones else ' '}] {milestone}")
            if goal.notes:
                lines.append(f"  Notes: {goal.notes}")
        return "\n".join(lines)


def get_goal_store(project_root: str) -> GoalStore:
    return GoalStore(pathlib.Path(project_root).resolve() / ".coderai" / "goals")


def handle_goal_tool(args: dict[str, Any], context: Any) -> ToolResult:
    """Manage the same validated goal contract advertised by the registry."""
    try:
        if (
            context is None
            or not getattr(context, "session_id", None)
            or not getattr(context, "project_root", None)
        ):
            raise ValueError("A session context is required for goals.")
        action = args.get("action")
        if action not in GOAL_ACTIONS:
            raise ValueError(f"action must be one of: {', '.join(GOAL_ACTIONS)}.")
        store = get_goal_store(context.project_root)
        sid = context.session_id
        if action == "status":
            return ToolResult(
                ok=True,
                name="goal",
                output=store.format_summary(sid),
                metadata={"goals": store.snapshot(sid)},
            )
        runner = getattr(getattr(context, "session_manager", None), "goal_runner", None)
        goal: Goal | None
        if action == "create":
            if (
                runner is not None
                and runner.is_driving(sid)
                and args.get("status", "running") == "running"
            ):
                raise ValueError(
                    "A goal is executing; create a pending goal instead of replacing its round budget."
                )
            goal = store.create(
                sid,
                _text(args.get("objective"), "objective", required=True),
                args.get("max_rounds"),
                args.get("notes", ""),
                description=args.get("description", ""),
                milestones=args.get("milestones", []),
                status=args.get("status", "running"),
            )
        else:
            goal_id = args.get("goal_id")
            if not goal_id:
                active = store.get_active_goal(sid)
                goal_id = active.id if active else None
            if not goal_id:
                raise ValueError("goal_id is required when no goal is running.")
            changes = {
                key: value
                for key, value in args.items()
                if key
                in {
                    "objective",
                    "description",
                    "status",
                    "max_rounds",
                    "notes",
                    "milestones",
                    "completed_milestones",
                }
            }
            status = {
                "start": "running",
                "pause": "paused",
                "complete": "completed",
                "cancel": "cancelled",
            }.get(action)
            if status:
                changes["status"] = status
            goal = store.update(
                sid, goal_id, expected_revision=args.get("expected_revision"), **changes
            )
            if goal is None:
                raise ValueError(f"Unknown goal '{goal_id}'.")
        assert goal is not None
        if (
            goal.status == "running"
            and (action in ("create", "start") or args.get("status") == "running")
            and runner is not None
        ):
            runner.request(sid, goal.id)
        return ToolResult(
            ok=True,
            name="goal",
            output=f"Goal [{goal.id}] {goal.status.upper()}: {goal.objective}",
            metadata={**goal.to_dict(), "goalSnapshot": store.snapshot(sid)},
        )
    except (ValueError, TypeError, OSError, TimeoutError) as exc:
        return ToolResult(ok=False, name="goal", error=str(exc))
