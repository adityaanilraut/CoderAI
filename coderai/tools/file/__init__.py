# Ported from coderai/core/tools/* (file group) - kimi structure (kimi_cli/tools/file/__init__.py).
"""File discovery tools: glob + grep (per-tool modules, Kimi `tools/file` parity)."""

from coderai.tools.file.glob import (
    GLOB_DESCRIPTION,
    glob_tool_definition,
    handle_glob,
    handle_glob_tool,
)
from coderai.tools.file.grep import (
    GREP_DESCRIPTION,
    grep_tool_definition,
    handle_grep,
    handle_grep_tool,
)

__all__ = (
    "GLOB_DESCRIPTION",
    "GREP_DESCRIPTION",
    "glob_tool_definition",
    "grep_tool_definition",
    "handle_glob",
    "handle_glob_tool",
    "handle_grep",
    "handle_grep_tool",
)
