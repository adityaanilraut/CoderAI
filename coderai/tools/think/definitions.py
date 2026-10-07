"""Built-in tool definitions owned by this subsystem."""

from __future__ import annotations
from typing import Any
from coderai.tools.legacy.policy import builtin_effect_policy
from coderai.tools import think as _think
from coderai.tools.legacy.schema import define_tool


def register_tools(registry: Any) -> None:
    registry.register(
        define_tool(
            effects=builtin_effect_policy("Think", False),
            name="Think",
            description="Think about something without obtaining new information or changing state. Appends the thought to the log. Use for complex reasoning or scratch memory.",
            parameters={
                "thought": {
                    "type": "string",
                    "description": "A thought to think about.",
                },
            },
            required=["thought"],
            handler=_think.handle_think_tool,
            category="meta",
            is_mutating=False,
            is_concurrency_safe=True,
        )
    )
