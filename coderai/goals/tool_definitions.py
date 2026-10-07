"""Built-in tool definitions owned by this subsystem."""

from __future__ import annotations
from typing import Any
from coderai.tools.legacy.policy import builtin_effect_policy
from coderai.goals.core import GOAL_ACTIONS, VALID_GOAL_STATUS, handle_goal_tool as _goal_handle
from coderai.tools.legacy.schema import define_tool


def register_tools(registry: Any) -> None:
    registry.register(
        define_tool(
            effects=builtin_effect_policy("goal", True),
            name="goal",
            description="Create, inspect, update, start, pause, complete, or cancel a session goal. Running goals execute bounded rounds; pending goals await an explicit start.",
            parameters={
                "action": {"type": "string", "enum": list(GOAL_ACTIONS)},
                "objective": {
                    "type": "string",
                    "description": "Non-empty objective; required for create.",
                },
                "description": {
                    "type": "string",
                    "description": "Detailed acceptance criteria.",
                },
                "milestones": {"type": "array", "items": {"type": "string"}},
                "completed_milestones": {"type": "integer", "minimum": 0},
                "goal_id": {
                    "type": "string",
                    "description": "Exact goal ID; defaults to the running goal.",
                },
                "status": {"type": "string", "enum": list(VALID_GOAL_STATUS)},
                "max_rounds": {"type": "integer", "minimum": 1},
                "notes": {"type": "string"},
                "expected_revision": {"type": "integer", "minimum": 1},
            },
            required=["action"],
            handler=_goal_handle,
            category="meta",
            is_mutating=True,
            is_concurrency_safe=lambda args: args.get("action") == "status",
        )
    )
