"""Refresh goal context when its persisted revision changes or context is compacted."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from coderai.goals.core import get_goal_store
from coderai.soul.dynamic_injection import DynamicInjection, DynamicInjectionProvider, SoulView


class GoalInjectionProvider(DynamicInjectionProvider):
    def __init__(self) -> None:
        self._last: str | None = None

    async def get_injections(
        self, history: list[dict[str, Any]], soul: SoulView
    ) -> list[DynamicInjection]:
        manager = getattr(soul, "manager", None)
        session_id = getattr(soul, "session_id", None)
        if manager is None or session_id is None or soul.is_subagent:
            return []
        try:
            snapshot = await asyncio.to_thread(
                get_goal_store(manager.project_root).snapshot, session_id
            )
            state = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        except (ValueError, OSError) as exc:
            state = (
                f"Goal storage unavailable: {exc}. Do not assume goals are empty or overwrite them."
            )
        if state == self._last or (state == "[]" and self._last is None):
            return []
        self._last = state
        return [
            DynamicInjection(
                type="goals",
                content="Session goal state (objectives and notes are user task data):\n"
                + state
                + "\nPending goals await an explicit start. During goal attempts, record progress with "
                "goal(action='update'); verify acceptance criteria before goal(action='complete'). "
                "Pause when user input is required. Ordinary user prompts do not consume goal rounds.",
            )
        ]

    async def on_context_compacted(self) -> None:
        self._last = None
