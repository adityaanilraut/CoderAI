"""Search tools for codebase exploration."""

import asyncio
import re
from pathlib import Path
from typing import Any, Dict, List

from .base import Tool


class TextSearchTool(Tool):
    """Tool for text-based codebase search."""

    name = "text_search"
    description = "Search the codebase for text patterns or code snippets"

    def get_parameters(self) -> Dict[str, Any]:
        """Get parameters schema."""
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query",
                },
                "base_path": {
                    "type": "string",
                    "description": "Base path to search from (default: current directory)",
                },
                "file_pattern": {
                    "type": "string",
                    "description": "File pattern to include (e.g., '*.py', '*.js')",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results (default: 20)",
                },
            },
            "required": ["query"],
        }

    async def execute(
        self,
        query: str,
        base_path: str = ".",
        file_pattern: str = "*",
        max_results: int = 20,
    ) -> Dict[str, Any]:
        """Search codebase."""
        try:
            base = Path(base_path).expanduser()
            if not base.exists():
                return {"success": False, "error": f"Base path not found: {base_path}"}

            results = []
            pattern = re.compile(re.escape(query), re.IGNORECASE)

            # Search recursively
            for file_path in base.rglob(file_pattern):
                if not file_path.is_file():
                    continue

                # Skip common ignore patterns
                if any(
                    p in file_path.parts
                    for p in [
                        ".git",
                        "node_modules",
                        "__pycache__",
                        ".venv",
                        "venv",
                        "dist",
                        "build",
                    ]
                ):
                    continue

                try:
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        for line_num, line in enumerate(f, 1):
                            if pattern.search(line):
                                results.append(
                                    {
                                        "file": str(
                                            file_path.relative_to(base)
                                            if file_path.is_relative_to(base)
                                            else file_path
                                        ),
                                        "line": line_num,
                                        "content": line.strip(),
                                    }
                                )
                                if len(results) >= max_results:
                                    break
                except Exception:
                    continue

                if len(results) >= max_results:
                    break

            return {
                "success": True,
                "query": query,
                "results": results,
                "count": len(results),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


class GrepTool(Tool):
    """Tool for pattern matching in files using grep."""

    name = "grep"
    description = "Search for patterns in files using grep"

    def get_parameters(self) -> Dict[str, Any]:
        """Get parameters schema."""
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Pattern to search for (supports regex)",
                },
                "path": {
                    "type": "string",
                    "description": "Path to search in (file or directory)",
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "Case insensitive search (default: false)",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "Search recursively in directories (default: true)",
                },
            },
            "required": ["pattern", "path"],
        }

    async def execute(
        self,
        pattern: str,
        path: str,
        case_insensitive: bool = False,
        recursive: bool = True,
    ) -> Dict[str, Any]:
        """Execute grep search using create_subprocess_exec to avoid shell injection."""
        try:
            cmd = ["grep", "-n"]
            if case_insensitive:
                cmd.append("-i")
            if recursive:
                cmd.append("-r")
            cmd.append(pattern)
            cmd.append(path)

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()

            output = stdout.decode("utf-8", errors="replace")
            matches = []

            for line in output.strip().split("\n"):
                if line:
                    # Parse grep output: filename:line_number:content
                    parts = line.split(":", 2)
                    if len(parts) >= 3:
                        matches.append(
                            {
                                "file": parts[0],
                                "line": int(parts[1]) if parts[1].isdigit() else 0,
                                "content": parts[2].strip(),
                            }
                        )

            return {
                "success": True,
                "pattern": pattern,
                "matches": matches,
                "count": len(matches),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
