"""Filesystem tools for file operations."""

import os
from glob import glob
from pathlib import Path
from typing import Any, Dict

from .base import Tool


class ReadFileTool(Tool):
    """Tool for reading file contents."""

    name = "read_file"
    description = "Read the contents of a file"

    def get_parameters(self) -> Dict[str, Any]:
        """Get parameters schema."""
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read",
                },
                "start_line": {
                    "type": "integer",
                    "description": "Optional starting line number (1-indexed)",
                },
                "end_line": {
                    "type": "integer",
                    "description": "Optional ending line number (1-indexed)",
                },
            },
            "required": ["path"],
        }

    async def execute(self, path: str, start_line: int = None, end_line: int = None) -> Dict[str, Any]:
        """Read file contents."""
        try:
            path_obj = Path(path).expanduser()
            if not path_obj.exists():
                return {"success": False, "error": f"File not found: {path}"}

            with open(path_obj, "r", encoding="utf-8") as f:
                if start_line is not None or end_line is not None:
                    lines = f.readlines()
                    start = (start_line - 1) if start_line else 0
                    end = end_line if end_line else len(lines)
                    content = "".join(lines[start:end])
                else:
                    content = f.read()

            return {
                "success": True,
                "path": str(path_obj),
                "content": content,
                "lines": len(content.split("\n")),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


class WriteFileTool(Tool):
    """Tool for writing/creating files."""

    name = "write_file"
    description = "Write content to a file (creates or overwrites)"

    def get_parameters(self) -> Dict[str, Any]:
        """Get parameters schema."""
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to write",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write to the file",
                },
            },
            "required": ["path", "content"],
        }

    async def execute(self, path: str, content: str) -> Dict[str, Any]:
        """Write content to file."""
        try:
            path_obj = Path(path).expanduser()
            path_obj.parent.mkdir(parents=True, exist_ok=True)

            with open(path_obj, "w", encoding="utf-8") as f:
                f.write(content)

            return {
                "success": True,
                "path": str(path_obj),
                "bytes_written": len(content.encode("utf-8")),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


class SearchReplaceTool(Tool):
    """Tool for search and replace in files."""

    name = "search_replace"
    description = "Search for text in a file and replace it"

    def get_parameters(self) -> Dict[str, Any]:
        """Get parameters schema."""
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file",
                },
                "search": {
                    "type": "string",
                    "description": "Text to search for",
                },
                "replace": {
                    "type": "string",
                    "description": "Text to replace with",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences (default: first only)",
                },
            },
            "required": ["path", "search", "replace"],
        }

    async def execute(
        self, path: str, search: str, replace: str, replace_all: bool = False
    ) -> Dict[str, Any]:
        """Search and replace in file."""
        try:
            path_obj = Path(path).expanduser()
            if not path_obj.exists():
                return {"success": False, "error": f"File not found: {path}"}

            with open(path_obj, "r", encoding="utf-8") as f:
                content = f.read()

            if search not in content:
                return {
                    "success": False,
                    "error": f"Search text not found in file",
                }

            if replace_all:
                new_content = content.replace(search, replace)
                count = content.count(search)
            else:
                new_content = content.replace(search, replace, 1)
                count = 1

            with open(path_obj, "w", encoding="utf-8") as f:
                f.write(new_content)

            return {
                "success": True,
                "path": str(path_obj),
                "replacements": count,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


class ListDirectoryTool(Tool):
    """Tool for listing directory contents."""

    name = "list_directory"
    description = "List files and directories in a path"

    def get_parameters(self) -> Dict[str, Any]:
        """Get parameters schema."""
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the directory",
                },
            },
            "required": ["path"],
        }

    async def execute(self, path: str) -> Dict[str, Any]:
        """List directory contents."""
        try:
            path_obj = Path(path).expanduser()
            if not path_obj.exists():
                return {"success": False, "error": f"Directory not found: {path}"}

            if not path_obj.is_dir():
                return {"success": False, "error": f"Not a directory: {path}"}

            entries = []
            for entry in sorted(path_obj.iterdir()):
                entries.append(
                    {
                        "name": entry.name,
                        "type": "directory" if entry.is_dir() else "file",
                        "size": entry.stat().st_size if entry.is_file() else 0,
                    }
                )

            return {
                "success": True,
                "path": str(path_obj),
                "entries": entries,
                "count": len(entries),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


class GlobSearchTool(Tool):
    """Tool for finding files using glob patterns."""

    name = "glob_search"
    description = "Find files matching a glob pattern"

    def get_parameters(self) -> Dict[str, Any]:
        """Get parameters schema."""
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern (e.g., '**/*.py', '*.txt')",
                },
                "base_path": {
                    "type": "string",
                    "description": "Base path to search from (default: current directory)",
                },
            },
            "required": ["pattern"],
        }

    async def execute(self, pattern: str, base_path: str = ".") -> Dict[str, Any]:
        """Find files matching pattern."""
        try:
            base = Path(base_path).expanduser()
            if not base.exists():
                return {"success": False, "error": f"Base path not found: {base_path}"}

            matches = []
            for match in base.glob(pattern):
                if match.is_file():
                    matches.append(str(match.relative_to(base) if match.is_relative_to(base) else match))

            return {
                "success": True,
                "pattern": pattern,
                "matches": matches,
                "count": len(matches),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

