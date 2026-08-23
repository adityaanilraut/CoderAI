"""Goal Round Driver — Autonomous multi-round continuation driver.

Directly ports the autonomous loop mechanics from DeepSeek Harness dsh-goal-round-driver.
"""

from __future__ import annotations

import logging
from typing import Any

from coderai.core.goals import Goal, GoalStore, get_goal_store

logger = logging.getLogger(__name__)


def render_goal_round_prompt(goal: Goal, round_num: int) -> str:
    """Render the autonomous round prompt injected at the start of each goal continuation round."""
    return f"""[GOAL MODE: AUTONOMOUS ROUND {round_num}/{goal.max_rounds}]
Objective: {goal.objective}
Goal ID: {goal.id} | Revision: {goal.revision}

Instructions for this round:
1. Continue execution autonomously towards the stated objective.
2. Execute required tool calls (edits, bash commands, tests, LSP inspections).
3. When the objective is completely satisfied and verified, invoke the `goal` tool with `action="complete"`.
4. If you require user input or cannot proceed, invoke the `goal` tool with `action="pause"`.
5. Do not stop prematurely if tasks remain unfulfilled.
"""


class GoalRoundDriver:
    """Coordinates autonomous multi-round execution for active goals in a session."""

    def __init__(self, project_root: str = ".") -> None:
        self.project_root = project_root
        self.store = get_goal_store(project_root)

    def should_continue_goal_round(self, session_id: str) -> tuple[bool, Goal | None, str | None]:
        """Check if an active running goal exists and has remaining rounds available.

        Returns (should_continue, active_goal, next_prompt).
        """
        active_goal = self.store.get_active_goal(session_id)
        if not active_goal:
            return False, None, None

        if active_goal.status != "running":
            return False, active_goal, None

        if active_goal.round >= active_goal.max_rounds:
            # Reached round budget
            self.store.update(
                session_id,
                active_goal.id,
                status="failed",
                notes="Exceeded maximum round budget without completion.",
            )
            return False, active_goal, None

        # Advance to next round
        next_goal = self.store.advance_round(session_id, active_goal.id)
        if not next_goal or next_goal.status != "running":
            return False, next_goal, None

        prompt = render_goal_round_prompt(next_goal, next_goal.round)
        return True, next_goal, prompt
