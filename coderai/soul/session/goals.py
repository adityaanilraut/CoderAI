"""Bounded goal execution and conversation snapshots for session lifecycle operations."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from coderai.goals.core import Goal, get_goal_store
from coderai.events import GOAL_STATE, SessionEvent

logger = logging.getLogger(__name__)
_SUCCESS_REASONS = {"natural", "max_steps", "max_iterations"}
_WAIT_REASONS = {"permission", "question", "waiting"}


def snapshot_from_history(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for row in reversed(rows):
        meta = row.get("meta") or (row.get("data") or {}).get("meta") or {}
        if "goalSnapshot" in meta:
            return meta["goalSnapshot"]
    return []


def publish_goal_state(
    manager: Any,
    session_id: str,
    text: str,
    *,
    notice: bool = False,
    run_state: dict[str, Any] | None = None,
) -> None:
    meta = {
        "isGoalContext": True,
        "goalSnapshot": get_goal_store(manager.project_root).snapshot(session_id),
        "goalRun": run_state,
    }
    # Keep notices out of the model surface while a tool call awaits permission.
    # The goal provider supplies current state before the next model step.
    manager._append_event(
        session_id,
        SessionEvent(
            seq=manager._next_seq(session_id),
            time=time.time() * 1000,
            type=GOAL_STATE,
            data={"content": text, "meta": meta},
        ),
    )
    if notice:
        message = manager._build_message(session_id, "assistant", text, meta=meta)
        manager.on_assistant_message(message, False)


@dataclass(frozen=True)
class GoalRequest:
    goal_id: str
    # True when the current turn already worked on the goal (tool create / resumed wait).
    include_current_turn: bool = True


class GoalRunner:
    """Own goal attempts independently of unrelated ordinary conversation turns."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager
        self._pending: dict[str, GoalRequest] = {}
        self._reasons: dict[str, str] = {}
        self._driving: dict[str, str] = {}
        self._lock = threading.RLock()

    def before_turn(self, session_id: str) -> None:
        """Recover an open permission/question round after restarting the process."""
        with self._lock:
            if session_id in self._driving or session_id in self._pending:
                return
            entry = self.manager._get_entry(session_id) or {}
            if entry.get("status") not in ("ask_permission", "waiting_for_user"):
                return
            for row in reversed(self.manager.session_store.read_rows(session_id)):
                if row.get("type") != GOAL_STATE:
                    continue
                state = (row.get("data", {}).get("meta") or {}).get("goalRun") or {}
                goal_id = state.get("goal_id")
                if state.get("waiting") and isinstance(goal_id, str):
                    goal = get_goal_store(self.manager.project_root).get(session_id, goal_id)
                    if goal and goal.status == "running":
                        self._pending[session_id] = GoalRequest(goal.id)
                return

    def interrupt(self, session_id: str) -> None:
        self.before_turn(session_id)
        with self._lock:
            request = self._pending.pop(session_id, None)
            self._reasons.pop(session_id, None)
        if request is not None:
            self._pause(session_id, request.goal_id, "interrupted while waiting")

    def is_driving(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._driving

    def stop_goal(self, session_id: str, goal_id: str) -> bool:
        """A change to an older goal must not interrupt the current goal's work."""
        with self._lock:
            request = self._pending.get(session_id)
            owned = self._driving.get(session_id) == goal_id or bool(
                request and request.goal_id == goal_id
            )
            if owned:
                self._pending.pop(session_id, None)
            return owned

    def request(self, session_id: str, goal_id: str) -> None:
        # Tool handlers run in worker threads; protect their handoff to the loop.
        with self._lock:
            if session_id not in self._driving:
                self._pending[session_id] = GoalRequest(goal_id)

    def snapshot(self, session_id: str) -> list[dict[str, Any]]:
        return get_goal_store(self.manager.project_root).snapshot(session_id)

    def on_turn_end(self, session_id: str, reason: str) -> None:
        with self._lock:
            if session_id in self._driving or session_id in self._pending:
                self._reasons[session_id] = reason

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._pending.pop(session_id, None)
            self._reasons.pop(session_id, None)

    def _pause(self, session_id: str, goal_id: str, reason: str) -> None:
        store = get_goal_store(self.manager.project_root)
        goal = store.get(session_id, goal_id)
        if goal and goal.status == "running":
            store.update(session_id, goal_id, status="paused")
            publish_goal_state(
                self.manager,
                session_id,
                f"Goal [{goal_id}] paused: {reason}. Use /goal start {goal_id} to resume.",
                notice=True,
            )

    def _finish_attempt(self, session_id: str, goal_id: str, reason: str | None) -> bool:
        if reason in _WAIT_REASONS:
            with self._lock:
                self._pending[session_id] = GoalRequest(goal_id)
            publish_goal_state(
                self.manager,
                session_id,
                f"Goal [{goal_id}] waiting for user input or permission; this round remains open.",
                notice=True,
                run_state={"goal_id": goal_id, "waiting": True},
            )
            return False
        if reason not in _SUCCESS_REASONS:
            self._pause(session_id, goal_id, reason or "turn ended without a completion outcome")
            return False
        store = get_goal_store(self.manager.project_root)
        goal = store.record_attempt(session_id, goal_id)
        if goal is None:
            return False
        if goal.status in ("failed", "completed"):
            publish_goal_state(
                self.manager,
                session_id,
                f"Goal [{goal.id}] {goal.status.upper()} after {goal.round}/{goal.max_rounds} completed rounds. {goal.notes}".strip(),
                notice=True,
            )
        return goal.status == "running"

    async def drain(self, session_id: str) -> None:
        """Called after activation; only explicit goal requests can start a driver."""
        with self._lock:
            if session_id in self._driving:
                return
            request = self._pending.pop(session_id, None)
            reason = self._reasons.pop(session_id, None)
        if request is not None:
            await self._run(session_id, request, reason)

    async def start(self, session_id: str, goal_id: str) -> None:
        if session_id in self._driving or session_id in self.manager._running_sessions:
            raise RuntimeError("Session already has an active turn.")
        if self.manager._get_entry(session_id) is None:
            raise ValueError("An existing session is required to start a goal.")
        goal = get_goal_store(self.manager.project_root).get(session_id, goal_id)
        if goal is None:
            raise ValueError(f"Unknown goal '{goal_id}'.")
        self.clear(session_id)
        await self._run(session_id, GoalRequest(goal_id, include_current_turn=False), None)

    async def _run(self, session_id: str, request: GoalRequest, reason: str | None) -> None:
        store = get_goal_store(self.manager.project_root)
        if session_id in self.manager._deleted_session_ids:
            return
        with store.execution_lock(session_id):
            with self._lock:
                if session_id in self._driving:
                    raise RuntimeError("A goal driver is already running.")
                self._driving[session_id] = request.goal_id
            try:
                if (self.manager._get_entry(session_id) or {}).get("planMode"):
                    self._pause(
                        session_id,
                        request.goal_id,
                        "plan mode requires user approval before execution",
                    )
                    return
                if request.include_current_turn:
                    if not self._finish_attempt(session_id, request.goal_id, reason):
                        return
                else:
                    goal = store.update(session_id, request.goal_id, status="running")
                    if goal is None:
                        raise ValueError(f"Unknown goal '{request.goal_id}'.")
                goal = store.get(session_id, request.goal_id)
                if goal is None or goal.status != "running":
                    return
                # Freeze this driver's maximum number of calls, even if a model edits its budget.
                remaining = goal.max_rounds - goal.round
                for _ in range(remaining):
                    if session_id in self.manager._deleted_session_ids:
                        return
                    if (self.manager._get_entry(session_id) or {}).get("planMode"):
                        self._pause(
                            session_id,
                            request.goal_id,
                            "plan mode requires user approval before execution",
                        )
                        return
                    goal = store.get(session_id, request.goal_id)
                    if goal is None or goal.status != "running":
                        return
                    if goal.round >= goal.max_rounds:
                        self._pause(session_id, goal.id, "round budget exhausted")
                        return
                    self._append_attempt(session_id, goal)
                    with self._lock:
                        self._reasons.pop(session_id, None)
                    await self.manager._activate(session_id)
                    with self._lock:
                        reason = self._reasons.pop(session_id, None)
                    if not self._finish_attempt(session_id, goal.id, reason):
                        return
                self._pause(session_id, request.goal_id, "this run's round limit was reached")
            except asyncio.CancelledError:
                self.clear(session_id)
                try:
                    self._pause(session_id, request.goal_id, "cancelled")
                except (ValueError, OSError):
                    logger.exception(
                        "Could not persist goal pause after cancellation for session %s", session_id
                    )
                raise
            except Exception as exc:
                self.clear(session_id)
                logger.exception("Goal execution failed for session %s", session_id)
                try:
                    self._pause(session_id, request.goal_id, str(exc))
                except (ValueError, OSError):
                    logger.exception(
                        "Could not persist goal pause after failure for session %s", session_id
                    )
                raise
            finally:
                with self._lock:
                    self._driving.pop(session_id, None)
                    self._reasons.pop(session_id, None)

    def _append_attempt(self, session_id: str, goal: Goal) -> None:
        publish_goal_state(
            self.manager,
            session_id,
            f"Goal [{goal.id}] running.",
            run_state={"goal_id": goal.id, "waiting": False},
        )
        checkpoint = self.manager.file_history.record_tracked_files_checkpoint(
            session_id, "Goal attempt checkpoint"
        )
        self.manager._append_message(
            self.manager._build_message(
                session_id,
                "user",
                f"Continue goal [{goal.id}], attempt {goal.round + 1}/{goal.max_rounds}.\n"
                f"{goal.objective}\n{goal.description}\n"
                f"Milestones: {json.dumps(list(goal.milestones), ensure_ascii=False)}\n"
                "Use goal(action='update') to record milestone progress. Verify the objective before "
                "goal(action='complete'). Pause if user input is needed.",
                meta={
                    "goalId": goal.id,
                    "goalSnapshot": get_goal_store(self.manager.project_root).snapshot(session_id),
                    "checkpointHash": checkpoint.checkpoint_hash,
                },
            )
        )
