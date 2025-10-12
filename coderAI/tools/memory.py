"""Memory tools for storing and recalling information."""

import json
from pathlib import Path
from typing import Any, Dict, List

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
        """Save memories to disk."""
        with open(self.memory_file, "w") as f:
            json.dump(self._memories, f, indent=2)

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


# Global memory store instance
memory_store = MemoryStore()


class SaveMemoryTool(Tool):
    """Tool for saving information to memory."""

    name = "save_memory"
    description = "Save information to persistent memory for later recall"

    def get_parameters(self) -> Dict[str, Any]:
        """Get parameters schema."""
        return {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Memory key/identifier",
                },
                "value": {
                    "type": "string",
                    "description": "Information to save",
                },
            },
            "required": ["key", "value"],
        }

    async def execute(self, key: str, value: str) -> Dict[str, Any]:
        """Save memory."""
        try:
            memory_store.add(key, value)
            return {
                "success": True,
                "key": key,
                "message": "Memory saved successfully",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


class RecallMemoryTool(Tool):
    """Tool for recalling information from memory."""

    name = "recall_memory"
    description = "Recall previously saved information from memory"

    def get_parameters(self) -> Dict[str, Any]:
        """Get parameters schema."""
        return {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Memory key to recall (optional, omit to search)",
                },
                "query": {
                    "type": "string",
                    "description": "Search query to find related memories",
                },
            },
            "required": [],
        }

    async def execute(self, key: str = None, query: str = None) -> Dict[str, Any]:
        """Recall memory."""
        try:
            if key:
                # Get specific memory
                value = memory_store.get(key)
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
                results = memory_store.search(query)
                return {
                    "success": True,
                    "query": query,
                    "results": results,
                    "count": len(results),
                }
            else:
                # List all memories
                memories = memory_store.list_all()
                return {
                    "success": True,
                    "memories": memories,
                    "count": len(memories),
                }
        except Exception as e:
            return {"success": False, "error": str(e)}

