"""Built-in tool definitions owned by this subsystem."""

from __future__ import annotations
from typing import Any
from coderai.tools.legacy.policy import builtin_effect_policy
from coderai.tools import dmail as _dmail
from coderai.tools.legacy.schema import define_tool


def register_tools(registry: Any) -> None:
    registry.register(
        define_tool(
            effects=builtin_effect_policy("SendDMail", False),
            name="SendDMail",
            description="Send a D-Mail: inject a time-leap directive into the running turn that the agent must obey immediately. El Psy Kongroo.",
            parameters={
                "message": {
                    "type": "string",
                    "description": "The directive to inject into the running turn.",
                },
                "checkpoint_id": {
                    "type": "integer",
                    "description": "Checkpoint to steer back to (0 = latest). Validated against recorded checkpoints.",
                },
            },
            required=["message"],
            handler=_dmail.handle_send_dmail_tool,
            category="meta",
            is_mutating=False,
            is_concurrency_safe=False,
        )
    )
