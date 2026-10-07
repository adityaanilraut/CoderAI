"""Built-in tool definitions owned by this subsystem."""

from __future__ import annotations
from typing import Any
from coderai.tools.legacy.policy import builtin_effect_policy
from coderai.tools import todo as _todo
from coderai.tools import todo as _plan
from coderai.tools.legacy.schema import define_tool


def register_tools(registry: Any) -> None:
    registry.register(
        define_tool(
            effects=builtin_effect_policy("UpdatePlan", True),
            name="UpdatePlan",
            description="Update the task plan and milestones.",
            parameters={
                "plan": {
                    "type": "string",
                    "description": "The updated markdown plan.",
                },
                "explanation": {
                    "type": "string",
                    "description": "Brief explanation of plan changes.",
                },
            },
            required=["plan"],
            handler=_plan.handle_update_plan_tool,
            category="meta",
            is_mutating=True,
            is_concurrency_safe=False,
        )
    )

    registry.register(
        define_tool(
            effects=builtin_effect_policy("todo_write", True),
            name="todo_write",
            description="Update the structured todo checklist for this session.",
            parameters={
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "content": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed", "cancelled"],
                            },
                        },
                        "required": ["content", "status"],
                    },
                },
                "explanation": {
                    "type": "string",
                    "description": "Brief explanation of the todo change.",
                },
            },
            required=["todos"],
            handler=_todo.handle_todo_write_tool,
            category="meta",
            is_mutating=True,
            is_concurrency_safe=False,
        )
    )
