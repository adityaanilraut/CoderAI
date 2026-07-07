"""DEPRECATED: Use ``search_replace`` with the ``edits`` parameter instead.

MultiEditTool is scheduled for removal in a future release.
"""

from typing import Any, Dict, List

from coderAI.tools.filesystem.edit import EditChunk  # noqa: F401  # re-export for compat
from coderAI.tools.filesystem import SearchReplaceTool as _SearchReplaceTool


class MultiEditTool(_SearchReplaceTool):
    """DEPRECATED: Use ``SearchReplaceTool`` with the ``edits`` parameter.

    This alias exists for backwards compatibility and delegates to
    ``SearchReplaceTool.execute(path=..., edits=...)``.
    """

    name = "multi_edit"
    description = (
        "DEPRECATED: Apply multiple search/replace edits to a file in a single atomic operation. "
        "Use the 'search_replace' tool with the 'edits' parameter instead."
    )

    async def execute(  # type: ignore[override]
        self, path: str, edits: List[Dict[str, Any]], **_ignored: Any
    ) -> Dict[str, Any]:
        import warnings

        warnings.warn(
            "MultiEditTool.execute() is deprecated; use SearchReplaceTool with edits= instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return await super().execute(path=path, edits=edits)
