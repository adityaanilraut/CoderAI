"""External Subagent Drivers & Backends."""

from coderai.core.subagent_backends.base import CliSubagentDriver
from coderai.core.subagent_backends.claude_code import (
    ClaudeCodeConfig,
    ClaudeCodeDriver,
)
from coderai.core.subagent_backends.codex import (
    CodexConfig,
    CodexDriver,
)
from coderai.core.subagent_backends.acp import AcpSubagentDriver

ClaudeCodeSubagentDriver = ClaudeCodeDriver
CodexSubagentDriver = CodexDriver

__all__ = [
    "CliSubagentDriver",
    "ClaudeCodeConfig",
    "ClaudeCodeDriver",
    "ClaudeCodeSubagentDriver",
    "CodexConfig",
    "CodexDriver",
    "CodexSubagentDriver",
    "AcpSubagentDriver",
]
