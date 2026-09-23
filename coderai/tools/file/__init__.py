from __future__ import annotations

from enum import Enum


class FileOpsWindow:
    """Maintains a window of file operations."""

    pass


class FileActions(str, Enum):
    READ = "read file"
    EDIT = "edit file"
    EDIT_OUTSIDE = "edit file outside of working directory"


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
    "FileActions",
    "FileOpsWindow",
    "GLOB_DESCRIPTION",
    "GREP_DESCRIPTION",
    "glob_tool_definition",
    "grep_tool_definition",
    "handle_glob",
    "handle_glob_tool",
    "handle_grep",
    "handle_grep_tool",
)
