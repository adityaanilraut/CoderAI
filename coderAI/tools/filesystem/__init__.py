# ruff: noqa: F401
"""Filesystem tools for file operations.

This package replaces the former ``coderAI/tools/filesystem.py`` monolith.
The full public *and* private surface of that module is re-exported here so
existing imports (``from coderAI.tools.filesystem import ReadFileTool``,
``... import _enforce_project_scope``) keep working unchanged.
"""

from coderAI.tools.undo import backup_store

from coderAI.tools.filesystem._guards import (
    DEFAULT_MAX_FILE_SIZE,
    DEFAULT_MAX_GLOB_RESULTS,
    PROTECTED_HOME_PATHS,
    PROTECTED_SYSTEM_PATHS,
    _allows_outside_project,
    _atomic_write_file,
    _emit_diff,
    _enforce_project_scope,
    _get_max_file_size,
    _get_max_glob_results,
    _is_in_project_root,
    _is_path_protected,
    _O_NOFOLLOW,
    _reject_symlink_leaf,
    _safe_open_no_symlink,
)
from coderAI.tools.filesystem.read_write import (
    ReadFileParams,
    ReadFileTool,
    WriteFileParams,
    WriteFileTool,
)
from coderAI.tools.filesystem.edit import (
    ApplyDiffParams,
    ApplyDiffTool,
    SearchReplaceParams,
    SearchReplaceTool,
)
from coderAI.tools.filesystem.manage import (
    CopyFileParams,
    CopyFileTool,
    CreateDirectoryParams,
    CreateDirectoryTool,
    DeleteFileParams,
    DeleteFileTool,
    GlobSearchParams,
    GlobSearchTool,
    ListDirectoryParams,
    ListDirectoryTool,
    MoveFileParams,
    MoveFileTool,
)
from coderAI.tools.filesystem.metadata import (
    FileChmodParams,
    FileChmodTool,
    FileChownParams,
    FileChownTool,
    FileReadlinkParams,
    FileReadlinkTool,
    FileStatParams,
    FileStatTool,
)
