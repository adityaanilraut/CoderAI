"""Built-in tool definitions owned by this subsystem."""

from __future__ import annotations
from typing import Any
from coderai.tools.legacy.policy import builtin_effect_policy
from coderai.tools.legacy import skill as _skill
from coderai.tools.legacy.schema import define_tool


def register_tools(registry: Any) -> None:
    registry.register(
        define_tool(
            effects=builtin_effect_policy("skill", False),
            name="skill",
            description="Load full instructions and examples for a specialized skill into active context.",
            parameters={
                "name": {
                    "type": "string",
                    "description": "Name of the skill to load (e.g. 'accidental-data-loss-prevention').",
                }
            },
            required=["name"],
            handler=_skill.handle_skill_tool,
            category="meta",
            is_mutating=False,
            is_concurrency_safe=True,
        )
    )
