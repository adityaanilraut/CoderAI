"""Memory tools for storing and recalling information."""

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from .base import Tool


class MemoryStore:
    """Simple JSON-based memory store."""

    def __init__(self):
        """Initialize memory store."""
        self.memory_dir = Path.home() / ".coderAI" / "memory"
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.memory_file = self.memory_dir / "memories.json"
        self._memories: Dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        """Load memories from disk."""
        if self.memory_file.exists():
            with open(self.memory_file, "r") as f:
                self._memories = json.load(f)

    def save(self) -> None:
        """Save memories to disk atomically."""
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self.memory_dir), prefix=".memories-", suffix=".json.tmp"
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self._memories, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.memory_file)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def add(self, key: str, value: Any) -> None:
        """Add or update a memory."""
        self._memories[key] = value
        self.save()

    def get(self, key: str) -> Any:
        """Get a memory by key."""
        return self._memories.get(key)

    def search(self, query: str) -> List[Dict[str, Any]]:
        """Search memories by query."""
        results = []
        query_lower = query.lower()
        for key, value in self._memories.items():
            if query_lower in key.lower() or (
                isinstance(value, str) and query_lower in value.lower()
            ):
                results.append({"key": key, "value": value})
        return results

    def list_all(self) -> Dict[str, Any]:
        """Get all memories."""
        return self._memories.copy()

    def delete(self, key: str) -> bool:
        """Delete a memory."""
        if key in self._memories:
            del self._memories[key]
            self.save()
            return True
        return False


# Lazy-initialized memory store to avoid side effects on import
_memory_store: "MemoryStore | None" = None


def get_memory_store() -> MemoryStore:
    """Get or create the global memory store (lazy init)."""
    global _memory_store
    if _memory_store is None:
        _memory_store = MemoryStore()
    return _memory_store


class SaveMemoryParams(BaseModel):
    key: str = Field(..., description="Memory key/identifier")
    value: str = Field(..., description="Information to save")


class SaveMemoryTool(Tool):
    """Tool for saving information to memory."""

    name = "save_memory"
    description = "Save information to persistent memory for later recall"
    parameters_model = SaveMemoryParams

    async def execute(self, key: str, value: str) -> Dict[str, Any]:
        """Save memory."""
        try:
            get_memory_store().add(key, value)
            return {
                "success": True,
                "key": key,
                "message": "Memory saved successfully",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


class RecallMemoryParams(BaseModel):
    key: Optional[str] = Field(None, description="Memory key to recall (optional, omit to search)")
    query: Optional[str] = Field(None, description="Search query to find related memories")

    @model_validator(mode='after')
    def check_at_least_one(self):
        if not self.key and not self.query:
            raise ValueError("Must provide either 'key' or 'query'")
        return self


class RecallMemoryTool(Tool):
    """Tool for recalling information from memory."""

    name = "recall_memory"
    description = "Recall previously saved information from memory"
    parameters_model = RecallMemoryParams
    is_read_only = True

    async def execute(self, key: str = None, query: str = None) -> Dict[str, Any]:
        """Recall memory."""
        try:
            store = get_memory_store()
            if key:
                # Get specific memory
                value = store.get(key)
                if value is None:
                    return {
                        "success": False,
                        "error": f"Memory not found: {key}",
                    }
                return {
                    "success": True,
                    "key": key,
                    "value": value,
                }
            elif query:
                # Search memories
                results = store.search(query)
                return {
                    "success": True,
                    "query": query,
                    "results": results,
                    "count": len(results),
                }
            else:
                # List all memories
                memories = store.list_all()
                return {
                    "success": True,
                    "memories": memories,
                    "count": len(memories),
                }
        except Exception as e:
            return {"success": False, "error": str(e)}



class DeleteMemoryParams(BaseModel):
    key: str = Field(..., description="Memory key to delete")


class DeleteMemoryTool(Tool):
    """Delete a specific memory entry."""

    name = "delete_memory"
    description = "Delete a previously saved memory entry by its key."
    category = "memory"
    parameters_model = DeleteMemoryParams
    requires_confirmation = True

    async def execute(self, key: str) -> Dict[str, Any]:
        try:
            store = get_memory_store()
            deleted = store.delete(key)
            if deleted:
                return {"success": True, "key": key, "message": f"Memory '{key}' deleted."}
            return {"success": False, "error": f"Memory key not found: '{key}'"}
        except Exception as e:
            return {"success": False, "error": str(e)}
