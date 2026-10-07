"""Built-in tool definitions owned by this subsystem."""

from __future__ import annotations
from typing import Any
from coderai.tools.legacy.policy import builtin_effect_policy
from coderai.tools import shell as _bash
from coderai.tools import shell as _pwsh
from coderai.tools.legacy.schema import define_tool

BASH_SCOPE_ENUM = [
    "read-in-cwd",
    "read-out-cwd",
    "write-in-cwd",
    "write-out-cwd",
    "delete-in-cwd",
    "delete-out-cwd",
    "query-git-log",
    "mutate-git-log",
    "network",
    "unknown",
]


def register_tools(registry: Any) -> None:
    registry.register(
        define_tool(
            effects=builtin_effect_policy("bash", True),
            name="bash",
            description=(
                "Execute shell commands, optionally in a persistent PTY session. "
                "Foreground commands default to 60 seconds (max 300) and move to a background job "
                "on timeout when job tools are available. Use timeout_ms or auto_background_on_timeout=false "
                "for a hard timeout. Background timeout defaults to 600 seconds (max 86400)."
            ),
            parameters={
                "command": {"type": "string", "description": "The shell command to execute"},
                "description": {
                    "type": "string",
                    "description": "Clear, concise description of what this command does in active voice.",
                },
                "sideEffects": {
                    "type": "array",
                    "description": "Permission scopes required by this bash command.",
                    "items": {"type": "string", "enum": sorted(BASH_SCOPE_ENUM)},
                },
                "run_in_background": {"type": "boolean"},
                "cwd": {
                    "type": "string",
                    "description": "Working directory; defaults to the session working directory.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds: foreground default 60, max 300; background default 600, max 86400.",
                },
                "disable_timeout": {
                    "type": "boolean",
                    "description": "Disable the timeout for explicitly background commands.",
                },
                "auto_background_on_timeout": {
                    "type": "boolean",
                    "description": "Set false to kill a foreground command at timeout instead of detaching it.",
                },
                "persistent": {
                    "type": "boolean",
                    "description": "Run inside a persistent PTY bash shell retaining variables and working directory across calls.",
                },
                "timeout_ms": {
                    "type": "integer",
                    "description": "Command execution timeout in milliseconds.",
                },
                "sandbox_permissions": {
                    "type": "string",
                    "description": "Escalated sandbox permissions mode if required.",
                },
                "justification": {
                    "type": "string",
                    "description": "Justification for requested sandbox escalation.",
                },
            },
            required=["command", "sideEffects"],
            handler=_bash.handle_bash_tool,
            category="shell",
            is_mutating=True,
            is_concurrency_safe=False,
        )
    )

    registry.register(
        define_tool(
            effects=builtin_effect_policy("pwsh", True),
            name="pwsh",
            description="Execute commands in a PowerShell session with background job and timeout support.",
            parameters={
                "command": {
                    "type": "string",
                    "description": "The PowerShell command or script to execute.",
                },
                "description": {
                    "type": "string",
                    "description": "Clear description of what this command does.",
                },
                "sideEffects": {
                    "type": "array",
                    "description": "Permission scopes required by this command.",
                    "items": {"type": "string", "enum": sorted(BASH_SCOPE_ENUM)},
                },
                "run_in_background": {"type": "boolean"},
                "timeout_ms": {
                    "type": "integer",
                    "description": "Command execution timeout in milliseconds.",
                },
            },
            required=["command", "sideEffects"],
            handler=_pwsh.handle_pwsh_tool,
            category="shell",
            is_mutating=True,
            is_concurrency_safe=False,
        )
    )
