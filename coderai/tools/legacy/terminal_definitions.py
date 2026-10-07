"""Built-in tool definitions owned by this subsystem."""

from __future__ import annotations
from typing import Any
from coderai.tools.legacy.policy import builtin_effect_policy
from coderai.tools.legacy import terminal as _terminal
from coderai.tools.legacy.schema import define_tool


def register_tools(registry: Any) -> None:
    registry.register(
        define_tool(
            effects=builtin_effect_policy("terminal_open", True),
            name="terminal_open",
            description="Open a persistent interactive terminal (PTY) session.",
            parameters={
                "type": {"type": "string", "enum": ["bash", "sh", "zsh", "pwsh"]},
                "name": {"type": "string"},
                "cwd": {"type": "string"},
            },
            required=[],
            handler=_terminal.handle_terminal_open_tool,
            category="shell",
            is_mutating=True,
            is_concurrency_safe=False,
        )
    )

    registry.register(
        define_tool(
            effects=builtin_effect_policy("terminal_send", True),
            name="terminal_send",
            description="Send input text to an active interactive terminal session.",
            parameters={
                "sessionId": {"type": "string"},
                "text": {"type": "string"},
                "submit": {"type": "boolean"},
                "run_in_background": {"type": "boolean"},
                "timeout_ms": {"type": "number"},
            },
            required=["sessionId", "text"],
            handler=_terminal.handle_terminal_send_tool,
            category="shell",
            is_mutating=True,
            is_concurrency_safe=False,
        )
    )

    registry.register(
        define_tool(
            effects=builtin_effect_policy("terminal_read", False),
            name="terminal_read",
            description="Read pending output from an active interactive terminal session.",
            parameters={
                "sessionId": {"type": "string"},
                "timeout_ms": {"type": "number"},
            },
            required=["sessionId"],
            handler=_terminal.handle_terminal_read_tool,
            category="shell",
            is_mutating=False,
            is_concurrency_safe=True,
        )
    )

    registry.register(
        define_tool(
            effects=builtin_effect_policy("terminal_signal", True),
            name="terminal_signal",
            description="Send a POSIX signal (e.g. SIGINT, SIGTERM, SIGKILL) to an active terminal.",
            parameters={
                "sessionId": {"type": "string"},
                "signal": {
                    "type": "string",
                    "enum": ["SIGINT", "SIGTERM", "SIGKILL", "SIGHUP"],
                },
            },
            required=["sessionId"],
            handler=_terminal.handle_terminal_signal_tool,
            category="shell",
            is_mutating=True,
            is_concurrency_safe=False,
        )
    )

    registry.register(
        define_tool(
            effects=builtin_effect_policy("terminal_close", True),
            name="terminal_close",
            description="Close and terminate an active persistent terminal session.",
            parameters={"sessionId": {"type": "string"}},
            required=["sessionId"],
            handler=_terminal.handle_terminal_close_tool,
            category="shell",
            is_mutating=True,
            is_concurrency_safe=False,
        )
    )

    registry.register(
        define_tool(
            effects=builtin_effect_policy("terminal_list", False),
            name="terminal_list",
            description="List all active persistent interactive terminal sessions.",
            parameters={},
            required=[],
            handler=_terminal.handle_terminal_list_tool,
            category="shell",
            is_mutating=False,
            is_concurrency_safe=True,
        )
    )
