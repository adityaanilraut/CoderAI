"""Standardized error codes for all tool results."""

from enum import Enum


class ToolErrorCode(str, Enum):
    """Standardized error codes for all tool results.

    Every tool must return one of these codes (or ``TOOL_ERROR`` as a
    generic fallback) in the ``"error_code"`` field when ``success`` is
    ``False``.  This lets the agent loop and downstream consumers reason
    about tool failures without parsing ad-hoc strings.
    """

    NOT_FOUND = "not_found"
    PERMISSION_DENIED = "permission_denied"
    TIMEOUT = "timeout"
    SCOPE = "scope"
    VALIDATION = "validation_error"
    IO = "io"
    TOOL_ERROR = "tool_error"
    DENIED = "denied"
    DENIED_BY_HOOK = "denied_by_hook"
    HOOK_BLOCKED = "hook_blocked"
    PARSE_ERROR = "parse_error"
    CANCELLED = "cancelled"
    TOOL_EXCEPTION = "tool_exception"
    TOO_LARGE = "too_large"
    SYMLINK = "symlink"
    NOT_GIT_REPO = "not_git_repo"
    SCOPE_MISMATCH = "scope_mismatch"
    UNSAFE_STAGING = "unsafe_staging"
    ALL_FILTERED = "all_filtered"
    BLOCKED = "blocked"
    INTERACTIVE = "interactive"
    MALFORMED_COMMAND = "malformed_command"
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    INVALID_TASK_ID = "invalid_task_id"
    MISSING_DIRECTORY = "missing_directory"
    NOT_A_DIRECTORY = "not_a_directory"
