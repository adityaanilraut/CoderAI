"""UI-agnostic CoderAI engine package.

Public surface:
    from coderai.core.session import SessionManager
    from coderai.core.permissions import compute_tool_call_permissions
    from coderai.core.settings import resolve_current_settings
"""

from coderai.core.openai_client import create_openai_client  # noqa: F401
from coderai.core.permissions import compute_tool_call_permissions  # noqa: F401
from coderai.core.session import SessionManager  # noqa: F401
from coderai.core.settings import resolve_current_settings  # noqa: F401
from coderai.core.state import FileSnippet, FileState, get_snippet  # noqa: F401
from coderai.core.tools import ToolExecutor, ToolResult  # noqa: F401

__all__ = [
    "SessionManager",
    "ToolExecutor",
    "ToolResult",
    "FileSnippet",
    "FileState",
    "compute_tool_call_permissions",
    "create_openai_client",
    "resolve_current_settings",
    "get_snippet",
]
