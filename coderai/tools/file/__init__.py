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
    Glob,
    glob_tool_definition,
    handle_glob,
    handle_glob_tool,
)
from coderai.tools.file.grep import (
    GREP_DESCRIPTION,
    Grep,
    grep_tool_definition,
    handle_grep,
    handle_grep_tool,
)
from coderai.tools.file.read import ReadFile
from coderai.tools.file.read_media import ReadMediaFile
from coderai.tools.file.replace import StrReplaceFile
from coderai.tools.file.write import WriteFile

__all__ = (
    "FileActions",
    "FileOpsWindow",
    "ReadFile",
    "ReadMediaFile",
    "Glob",
    "Grep",
    "WriteFile",
    "StrReplaceFile",
    "GLOB_DESCRIPTION",
    "GREP_DESCRIPTION",
    "glob_tool_definition",
    "grep_tool_definition",
    "handle_glob",
    "handle_glob_tool",
    "handle_grep",
    "handle_grep_tool",
)
