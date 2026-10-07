"""Built-in tool definitions owned by this subsystem."""

from __future__ import annotations
from typing import Any
from coderai.tools.legacy.policy import builtin_effect_policy
from coderai.tools.legacy import schedule as _schedule
from coderai.tools.legacy.schema import define_tool


def register_tools(registry: Any) -> None:
    registry.register(
        define_tool(
            effects=builtin_effect_policy("schedule_create", True),
            name="schedule_create",
            description="Schedule a reminder or background instruction (one-shot or recurring).",
            parameters={
                "prompt": {
                    "type": "string",
                    "description": "The instruction prompt to execute when triggered.",
                },
                "after_seconds": {
                    "type": "number",
                    "description": "Seconds to wait for a one-shot timer.",
                },
                "at": {
                    "type": "string",
                    "description": "ISO 8601 / RFC3339 timestamp for a one-shot schedule.",
                },
                "every_seconds": {
                    "type": "number",
                    "description": "Interval in seconds for a recurring schedule (minimum 300).",
                },
            },
            required=["prompt"],
            handler=_schedule.handle_schedule_create_tool,
            category="meta",
            is_mutating=True,
            is_concurrency_safe=False,
        )
    )

    registry.register(
        define_tool(
            effects=builtin_effect_policy("schedule_list", False),
            name="schedule_list",
            description="List all scheduled timers and cron jobs.",
            parameters={},
            required=[],
            handler=_schedule.handle_schedule_list_tool,
            category="meta",
            is_mutating=False,
            is_concurrency_safe=True,
        )
    )

    registry.register(
        define_tool(
            effects=builtin_effect_policy("schedule_delete", True),
            name="schedule_delete",
            description="Delete an active timer or cron schedule by ID.",
            parameters={"schedule_id": {"type": "string"}},
            required=["schedule_id"],
            handler=_schedule.handle_schedule_delete_tool,
            category="meta",
            is_mutating=True,
            is_concurrency_safe=False,
        )
    )
