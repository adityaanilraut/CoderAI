"""Built-in tool definitions owned by this subsystem."""

from __future__ import annotations
from typing import Any
from coderai.tools.legacy.policy import builtin_effect_policy
from coderai.tools import ask_user as _ask
from coderai.tools.legacy.schema import define_tool


def register_tools(registry: Any) -> None:
    registry.register(
        define_tool(
            effects=builtin_effect_policy("AskUserQuestion", False),
            name="AskUserQuestion",
            description="Prompt the user with structured questions, choices, or clarifications.",
            parameters={
                "questions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string"},
                            "options": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {"type": "string"},
                                        "description": {"type": "string"},
                                    },
                                    "required": ["label"],
                                },
                            },
                            "multiSelect": {"type": "boolean"},
                        },
                        "required": ["question", "options"],
                    },
                    "description": "List of structured questions to present to the user.",
                }
            },
            required=["questions"],
            handler=_ask.handle_ask_user_question_tool,
            category="interactive",
            is_mutating=False,
            is_concurrency_safe=False,
        )
    )
