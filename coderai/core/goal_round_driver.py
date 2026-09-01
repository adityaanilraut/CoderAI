"""Same-session goal-round driver (harness parity, compact).

Queues one automatic continuation round for an active, armed goal whenever the
session reaches quiescence: the agent's last turn ended cleanly, the round cap
is not exhausted, and no interruption is pending. Blocks the goal at the round
limit with code ``round-limit`` and disarms on failed/queuing errors — the
durable phase is preserved, the automatic authority is not.

Hooked from ``SessionManager.reply_session`` so rounds continue in the same
session (``render_goal_round_prompt`` mirrors the harness prompt verbatim).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from coderai.core.goals_dsh import (
    BLOCK_CODE_QUEUE_FAILED,
    BLOCK_CODE_ROUND_LIMIT,
    GoalBlockReason,
    get_dsh_goal_store,
)

logger = logging.getLogger(__name__)


def render_goal_round_prompt(goal: Any, round_number: int) -> str:
    """Model-visible continuation prompt for one same-session goal round."""
    return (
        "<goal_round>\n"
        f"Objective: {json.dumps(goal.objective)}\n"
        f"Round: {round_number}/{goal.max_goal_rounds}\n\n"
        "Continue working toward the objective in this same session. Treat the current workspace, "
        "tool results, and durable session state as authoritative; inspect them instead of assuming "
        "earlier narration is still current. Make concrete progress and verify the result. Before "
        "claiming completion, gather evidence that the whole objective is achieved, read the current "
        "goal, and mark it complete. If work remains, leave the goal active for the next round. Follow "
        "the configured goal-tool policy before reporting a blocker.\n"
        "</goal_round>"
    )


def maybe_queue_goal_round(manager: Any, session_id: str) -> bool:
    """Reserve at most one next goal round; appends its prompt as a user message.

    Returns True when a round was queued (the caller should re-activate the
    session), False otherwise.
    """
    try:
        settings = manager.get_resolved_settings() or {}
    except Exception:  # pragma: no cover - defensive
        settings = {}
    orch = settings.get("orchestration") or {}
    if orch.get("goalAutoRounds", True) is False:
        return False

    store = get_dsh_goal_store(manager.project_root)
    goal = store.get(session_id)
    if goal is None:
        return False
    if goal.phase != "active" or goal.activation != "armed":
        return False

    if goal.rounds_started >= goal.max_goal_rounds:
        try:
            store.block(
                session_id,
                goal.ref(),
                GoalBlockReason(
                    code=BLOCK_CODE_ROUND_LIMIT,
                    message=(
                        f"Goal reached its configured limit of "
                        f"{goal.max_goal_rounds} rounds."
                    ),
                ),
            )
        except Exception:  # pragma: no cover - defensive
            pass
        return False

    round_number = store.admit_round(session_id)
    if round_number is None:
        return False
    prompt = render_goal_round_prompt(goal, round_number)
    try:
        manager._append_message(
            manager._build_message(
                session_id,
                "user",
                prompt,
                meta={
                    "source": "goal-round",
                    "goalRound": round_number,
                    "goalId": goal.id,
                    "goalRevision": goal.revision,
                },
            )
        )
    except Exception as exc:  # pragma: no cover - queue failure containment
        logger.warning("goal-round-driver: could not queue round for %s: %s", session_id, exc)
        try:
            store.block(
                session_id,
                goal.ref(),
                GoalBlockReason(
                    code=BLOCK_CODE_QUEUE_FAILED,
                    message=f"Could not queue goal round {round_number}: {exc}",
                ),
            )
        except Exception:
            pass
        return False
    store.set_in_goal_round(session_id, True)
    return True


def finish_goal_round(
    session_id: str, project_root: str = ".", entry_status: str | None = None
) -> None:
    """End-of-round bookkeeping: clear the in-round flag; disarm on hard ends."""
    store = get_dsh_goal_store(project_root)
    store.set_in_goal_round(session_id, False)
    if entry_status in ("failed", "interrupted"):
        store.disarm(session_id)
