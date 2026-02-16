"""Filesystem tools for file operations."""

import os
from glob import glob
from pathlib import Path
from typing import Any, Dict

from .base import Tool

# Maximum file size (1 MB) that can be read at once
MAX_FILE_SIZE = 1_048_576

# Maximum number of glob results to prevent context overflow
MAX_GLOB_RESULTS = 200

# Paths that tools should never write to
PROTECTED_PATHS = [
    ".ssh",
    ".gnupg",
    ".aws",
    ".config/gcloud",
    ".kube",
    ".docker",
    ".bash_history",
    ".zsh_history",
]


def _is_path_protected(path: Path) -> bool:
    """Check if a path targets a protected location."""
    resolved = path.resolve()
    home = Path.home()
    for protected in PROTECTED_PATHS:
        protected_path = (home / protected).resolve()
        try:
            resolved.relative_to(protected_path)
            return True
        except ValueError:
            continue
    return False


class ReadFileTool(Tool):
    """Tool for reading file contents."""

    name = "read_file"
    description = "Read the contents of a file (max 1MB)"

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
        """Read file contents with size limit."""
        try:
            path_obj = Path(path).expanduser()
            if not path_obj.exists():
                return {
                    "success": False,
                    "error": f"File not found: {path}",
                    "hint": "Use list_directory or glob_search to find the correct path.",
                }

            if not path_obj.is_file():
                return {
                    "success": False,
                    "error": f"Not a file: {path}",
                    "hint": "Use list_directory for directories.",
                }

            # Check file size before reading
            file_size = path_obj.stat().st_size
            if file_size > MAX_FILE_SIZE:
                return {
                    "success": False,
                    "error": f"File too large: {file_size:,} bytes (limit: {MAX_FILE_SIZE:,} bytes).",
                    "hint": "Use start_line and end_line to read a specific range, or use grep to search.",
                }

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
                "size_bytes": file_size,
            }
        except UnicodeDecodeError:
            return {
                "success": False,
                "error": f"Cannot read binary file: {path}",
                "hint": "This appears to be a binary file. Use run_command with appropriate tools like 'file', 'hexdump', etc.",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


class WriteFileTool(Tool):
    """Tool for writing/creating files."""

    name = "write_file"
    description = "Write content to a file (creates or overwrites). Protected system paths are blocked."

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
        """Write content to file with path protection."""
        try:
            path_obj = Path(path).expanduser()

            # Check path protection
            if _is_path_protected(path_obj):
                return {
                    "success": False,
                    "error": f"Cannot write to protected path: {path}",
                    "hint": "This path is protected for security. Choose a different location.",
                }

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
        """Search and replace in file with protection."""
        try:
            path_obj = Path(path).expanduser()
            if not path_obj.exists():
                return {
                    "success": False,
                    "error": f"File not found: {path}",
                    "hint": "Check the path with list_directory or glob_search.",
                }

            if _is_path_protected(path_obj):
                return {
                    "success": False,
                    "error": f"Cannot modify protected path: {path}",
                }

            with open(path_obj, "r", encoding="utf-8") as f:
                content = f.read()

            if search not in content:
                return {
                    "success": False,
                    "error": "Search text not found in file",
                    "hint": "Use text_search or grep to verify the exact text in the file.",
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
                return {
                    "success": False,
                    "error": f"Directory not found: {path}",
                    "hint": "Check the parent directory with list_directory.",
                }

            if not path_obj.is_dir():
                return {
                    "success": False,
                    "error": f"Not a directory: {path}",
                    "hint": "Use read_file to read file contents.",
                }

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
    description = f"Find files matching a glob pattern (max {MAX_GLOB_RESULTS} results)"

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
        """Find files matching pattern with result limit."""
        try:
            base = Path(base_path).expanduser()
            if not base.exists():
                return {
                    "success": False,
                    "error": f"Base path not found: {base_path}",
                    "hint": "Check the path with list_directory.",
                }

            matches = []
            total_matches = 0
            for match in base.glob(pattern):
                if match.is_file():
                    # Skip common ignore patterns
                    if any(
                        p in match.parts
                        for p in [".git", "node_modules", "__pycache__", ".venv", "venv"]
                    ):
                        continue

                    total_matches += 1
                    if len(matches) < MAX_GLOB_RESULTS:
                        matches.append(
                            str(match.relative_to(base) if match.is_relative_to(base) else match)
                        )

            result = {
                "success": True,
                "pattern": pattern,
                "matches": matches,
                "count": len(matches),
            }

            if total_matches > MAX_GLOB_RESULTS:
                result["note"] = (
                    f"Showing {MAX_GLOB_RESULTS} of {total_matches} total matches. "
                    "Use a more specific pattern to narrow results."
                )

            return result
        except Exception as e:
            return {"success": False, "error": str(e)}
