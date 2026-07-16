"""Shared leaf types used by tools, core, and context.

Kept dependency-free so ``tools`` can import these without depending on
``core`` orchestration (avoids the historical core↔tools cycle).
"""

from coderAI.types.provenance import (
    Provenance,
    UNTRUSTED_CLOSE_TAG,
    UNTRUSTED_OPEN_TAG,
    fence_project_context,
    wrap_untrusted_output,
)
from coderAI.types.tool_error_codes import ToolErrorCode
from coderAI.types.tool_results import normalize_tool_result

__all__ = [
    "Provenance",
    "ToolErrorCode",
    "UNTRUSTED_CLOSE_TAG",
    "UNTRUSTED_OPEN_TAG",
    "fence_project_context",
    "normalize_tool_result",
    "wrap_untrusted_output",
]
