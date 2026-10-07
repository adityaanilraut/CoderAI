"""Built-in tool definitions owned by this subsystem."""

from __future__ import annotations
from typing import Any
from coderai.tools.legacy.policy import builtin_effect_policy
from coderai.tools import plan as _plan_mode
from coderai.tools.legacy.schema import define_tool


def register_tools(registry: Any) -> None:
    registry.register(
        define_tool(
            effects=builtin_effect_policy("exit_plan_mode", False),
            name="exit_plan_mode",
            description=(
                "Exit plan mode and present the finalized plan to the user for approval. "
                "Provide a concise implementation summary in the `summary` parameter before regular "
                "mutation and execution tools are reactivated."
            ),
            parameters={
                "plan": {"type": "string"},
                "summary": {
                    "type": "string",
                    "description": (
                        "Concise summary of the planned approach and key decisions "
                        "presented to the user for plan approval."
                    ),
                },
            },
            required=[],
            handler=_plan_mode.handle_exit_plan_mode_tool,
            category="meta",
            is_mutating=False,
            is_concurrency_safe=False,
        )
    )

    registry.register(
        define_tool(
            effects=builtin_effect_policy("enter_plan_mode", False),
            name="enter_plan_mode",
            description=_plan_mode.ENTER_PLAN_MODE_DESCRIPTION,
            parameters={},
            required=[],
            handler=_plan_mode.handle_enter_plan_mode_tool,
            category="meta",
            is_mutating=False,
            is_concurrency_safe=False,
        )
    )
