"""Backward-compatibility shim for file snippet tracking.

All file snippet models and tracking functions have been moved to
:mod:`coderai.file_snippets` (AL-B2).
"""

from __future__ import annotations

from coderai.file_snippets import (
    FileSnippet,
    FileState,
    SessionStateManager,
    _refresh_rebuilt_file_state,
    clear_session_state,
    create_full_file_snippet,
    create_snippet,
    get_file_state,
    get_file_version,
    get_snippet,
    has_session_state,
    has_snippet_outdated_file_version,
    is_absolute_file_path,
    is_full_file_view,
    mark_file_read,
    normalize_file_path,
    rebuild_session_state_from_history,
    record_file_state,
    restore_snippet,
    was_file_read,
)

__all__ = [
    "FileSnippet",
    "FileState",
    "SessionStateManager",
    "clear_session_state",
    "create_full_file_snippet",
    "create_snippet",
    "get_file_state",
    "get_file_version",
    "get_snippet",
    "has_session_state",
    "has_snippet_outdated_file_version",
    "is_absolute_file_path",
    "is_full_file_view",
    "mark_file_read",
    "normalize_file_path",
    "rebuild_session_state_from_history",
    "record_file_state",
    "restore_snippet",
    "was_file_read",
]
