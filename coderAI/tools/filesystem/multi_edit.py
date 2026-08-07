"""Atomic multi-edit tool — standalone alias for batch search/replace.

Provides a dedicated ``multi_edit`` tool name (Claude Code parity) that
applies N search/replace edits in a single file lock acquisition. The
implementation delegates to SearchReplaceTool batch mode so there is a
single semantic implementation. Exists as a separate Tool subclass so
capability routing and persona filters can enable/disable it independently.
"""

from typing import Any, Optional

from pydantic import BaseModel, Field

from coderAI.tools.base import Tool
from coderAI.tools.filesystem.edit import EditChunk, SearchReplaceTool


class MultiEditParams(BaseModel):
    path: str = Field(..., description="Path to the file to edit")
    edits: list[EditChunk] = Field(
        ..., description="List of search/replace edits to apply atomically"
    )


class MultiEditTool(Tool):
    """Atomic multi-edit: apply multiple search/replace operations in one write."""

    name = "multi_edit"
    description = (
        "Apply multiple search/replace edits to a single file atomically. "
        "All edits succeed or none are written. More efficient than N sequential search_replace calls. "
        "Each edit requires a non-empty search string and is applied sequentially on the in-memory content."
    )
    parameters_model = MultiEditParams
    requires_confirmation = True
    category = "filesystem"
    batch_serialize_by_path = True

    def __init__(self) -> None:
        super().__init__()
        self._delegate = SearchReplaceTool()

    def preview(self, arguments: dict[str, Any], original: Optional[str]) -> Any:
        return self._delegate.preview(arguments, original)

    async def execute(  # type: ignore[override]
        self,
        path: str,
        edits: list[dict[str, Any]],
    ) -> dict[str, Any]:
        # Normalize edits to dicts (Pydantic may give us model instances)
        norm_edits: list[dict[str, Any]] = []
        for e in edits or []:
            if isinstance(e, dict):
                norm_edits.append(e)
            elif hasattr(e, "model_dump"):
                norm_edits.append(e.model_dump())  # type: ignore[union-attr]
            else:
                norm_edits.append(dict(e))
        return await self._delegate.execute(path=path, search="", replace="", edits=norm_edits)
