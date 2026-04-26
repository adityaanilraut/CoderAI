from typing import Any, Dict, List
import os
import tempfile
from pathlib import Path
from pydantic import BaseModel, Field

from .base import Tool
from .filesystem import _is_path_protected, _enforce_project_scope, _emit_diff
from .undo import backup_store
from ..locks import resource_manager

class EditChunk(BaseModel):
    search: str = Field(..., description="Exact text to search for")
    replace: str = Field(..., description="Text to replace it with")
    expected_count: int = Field(1, description="Expected number of occurrences to replace")

class MultiEditParams(BaseModel):
    path: str = Field(..., description="Path to the file to edit")
    edits: List[EditChunk] = Field(..., description="List of search/replace operations to apply sequentially")

class MultiEditTool(Tool):
    """Tool for applying multiple string replacements in a single atomic write."""
    
    name = "multi_edit"
    description = "Apply multiple search/replace edits to a file in a single atomic operation."
    parameters_model = MultiEditParams
    requires_confirmation = True

    async def execute(self, path: str, edits: List[Dict[str, Any]]) -> Dict[str, Any]:
        try:
            path_obj = Path(path).expanduser()
            lock = await resource_manager.get_file_lock(str(path_obj))
            async with lock:
                if not path_obj.exists():
                    return {"success": False, "error": f"File not found: {path}"}
                if _is_path_protected(path_obj):
                    return {"success": False, "error": f"Cannot modify protected path: {path}"}
                scope_err = _enforce_project_scope(path_obj, "multi_edit")
                if scope_err:
                    return scope_err

                with open(path_obj, "r", encoding="utf-8") as f:
                    original_content = f.read()

                new_content = original_content
                
                for i, edit in enumerate(edits):
                    search = edit["search"]
                    replace = edit["replace"]
                    expected_count = edit.get("expected_count", 1)
                    
                    actual_count = new_content.count(search)
                    if actual_count == 0:
                        return {
                            "success": False,
                            "error": f"Edit {i+1} failed: expected to find search text, found 0 occurrences.",
                            "hint": "Check the file contents and make sure the search text exactly matches what's in the file."
                        }
                    
                    new_content = new_content.replace(search, replace)

                backup_store.backup_file(str(path_obj), "modify")
                
                fd, tmp_path = tempfile.mkstemp(dir=path_obj.parent, prefix=".tmp-")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(new_content)
                os.replace(tmp_path, str(path_obj))

                _emit_diff(path_obj, original_content, new_content)

                return {
                    "success": True,
                    "path": str(path_obj),
                    "edits_applied": len(edits),
                    "actual_counts": [new_content.count(edit["replace"]) for edit in edits],
                    "count_mismatches": [
                        {
                            "edit_index": i,
                            "expected_count": edit.get("expected_count", 1),
                            "actual_count": original_content.count(edit["search"]),
                        }
                        for i, edit in enumerate(edits)
                        if original_content.count(edit["search"]) != edit.get("expected_count", 1)
                    ],
                }
        except Exception as e:
            return {"success": False, "error": str(e)}
