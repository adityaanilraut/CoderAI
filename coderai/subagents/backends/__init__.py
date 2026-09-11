"""External Subagent Drivers & Backends."""

from coderai.subagents.backends.base import CliSubagentDriver
from coderai.subagents.backends.claude_code import (
    ClaudeCodeConfig,
    ClaudeCodeDriver,
)
from coderai.subagents.backends.codex import (
    CodexConfig,
    CodexDriver,
)
from coderai.subagents.backends.acp import AcpSubagentDriver

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
