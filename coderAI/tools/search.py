"""Search tools for codebase exploration."""

import asyncio
import ast
import re
from pathlib import Path
from typing import Any, Dict, List

from pydantic import BaseModel, Field

from .base import Tool


class TextSearchParams(BaseModel):
    query: str = Field(..., description="Search query")
    regex: bool = Field(False, description="Treat query as a regex (default: false → literal match)")
    base_path: str = Field(".", description="Base path to search from (default: current directory)")
    file_pattern: str = Field("*", description="File pattern to include (e.g., '*.py', '*.js')")
    max_results: int = Field(20, description="Maximum number of results (default: 20)")


class TextSearchTool(Tool):
    """Tool for text-based codebase search."""

    name = "text_search"
    description = (
        "Search file contents by literal text or regex. Use this when you know part of the "
        "text you want to find, like an error message, config key, or function call. "
        "Do not use this for symbol-aware lookups when you need definitions by name; use "
        "symbol_search instead. Example: query='TODO', file_pattern='*.py'."
    )
    parameters_model = TextSearchParams
    is_read_only = True

    async def execute(
        self,
        query: str,
        regex: bool = False,
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
            was_truncated = False
            if regex:
                try:
                    pattern = re.compile(query, re.IGNORECASE)
                except re.error as e:
                    return {"success": False, "error": f"Invalid regex: {e}"}
            else:
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
                                    was_truncated = True
                                    break
                except Exception:
                    continue

                if len(results) >= max_results:
                    was_truncated = True
                    break

            return {
                "success": True,
                "query": query,
                "results": results,
                "count": len(results),
                "was_truncated": was_truncated,
                "next_offset": len(results) if was_truncated else None,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


class GrepParams(BaseModel):
    pattern: str = Field(..., description="Pattern to search for (supports regex)")
    path: str = Field(..., description="Path to search in (file or directory)")
    case_insensitive: bool = Field(False, description="Case insensitive search (default: false)")
    recursive: bool = Field(True, description="Search recursively in directories (default: true)")
    max_results: int = Field(50, description="Maximum number of matches to return (default: 50)")


class GrepTool(Tool):
    """Tool for pattern matching in files using grep."""

    name = "grep"
    description = (
        "Search files with grep-compatible patterns. Use this when you want fast regex or "
        "line-based matching across a path. Do not use it for semantic symbol lookup; use "
        "symbol_search for that. Example: pattern='class Foo', path='src'."
    )
    parameters_model = GrepParams
    is_read_only = True

    async def execute(
        self,
        pattern: str,
        path: str,
        case_insensitive: bool = False,
        recursive: bool = True,
        max_results: int = 50,
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
                    parts = line.split(":", 2)
                    if len(parts) >= 3:
                        matches.append(
                            {
                                "file": parts[0],
                                "line": int(parts[1]) if parts[1].isdigit() else 0,
                                "content": parts[2].strip(),
                            }
                        )
                        if len(matches) >= max_results:
                            break

            truncated = len(output.strip().split("\n")) > len(matches)
            result = {
                "success": True,
                "pattern": pattern,
                "matches": matches,
                "count": len(matches),
                "was_truncated": truncated,
                "next_offset": len(matches) if truncated else None,
            }
            if truncated:
                result["note"] = f"Results capped at {max_results}. Use a more specific pattern to narrow results."
            return result
        except Exception as e:
            return {"success": False, "error": str(e)}


class SymbolSearchParams(BaseModel):
    symbol: str = Field(..., description="Symbol name to find, such as a function, class, or variable.")
    kind: str = Field("any", description="Optional symbol kind filter: any, function, class, method, variable.")
    path: str = Field(".", description="File or directory to search.")
    max_results: int = Field(20, description="Maximum number of results to return.")


class SymbolSearchTool(Tool):
    """Best-effort symbol-aware search for Python and TS/TSX projects."""

    name = "symbol_search"
    description = (
        "Find symbol definitions by name in Python and TS/TSX files. Use this when you know "
        "the symbol name and want the defining locations instead of raw text matches. Do not "
        "use it for arbitrary prose search; use text_search or grep there. Example: symbol='Agent', kind='class'."
    )
    parameters_model = SymbolSearchParams
    is_read_only = True

    async def execute(
        self,
        symbol: str,
        kind: str = "any",
        path: str = ".",
        max_results: int = 20,
    ) -> Dict[str, Any]:
        try:
            base = Path(path).expanduser()
            if not base.exists():
                return {"success": False, "error": f"Path not found: {path}"}

            files = [base] if base.is_file() else [p for p in base.rglob("*") if p.is_file()]
            results: List[Dict[str, Any]] = []
            was_truncated = False
            for file_path in files:
                if any(part in file_path.parts for part in (".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build")):
                    continue
                suffix = file_path.suffix.lower()
                if suffix == ".py":
                    matches = self._search_python(file_path, symbol, kind)
                elif suffix in {".ts", ".tsx", ".js", ".jsx"}:
                    matches = self._search_jsts(file_path, symbol, kind)
                else:
                    continue
                for match in matches:
                    results.append(match)
                    if len(results) >= max_results:
                        was_truncated = True
                        break
                if len(results) >= max_results:
                    break
            return {
                "success": True,
                "symbol": symbol,
                "kind": kind,
                "results": results,
                "count": len(results),
                "was_truncated": was_truncated,
                "next_offset": len(results) if was_truncated else None,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _search_python(self, file_path: Path, symbol: str, kind: str) -> List[Dict[str, Any]]:
        try:
            source = file_path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except Exception:
            return []

        wanted = kind.lower()
        results: List[Dict[str, Any]] = []

        for node in ast.walk(tree):
            node_kind = None
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                node_kind = "function"
            elif isinstance(node, ast.ClassDef):
                node_kind = "class"
            elif isinstance(node, ast.Assign):
                node_kind = "variable"
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if symbol in targets and wanted in {"any", "variable"}:
                    results.append({
                        "file": str(file_path),
                        "line": getattr(node, "lineno", 1),
                        "column": getattr(node, "col_offset", 0),
                        "name": symbol,
                        "kind": "variable",
                    })
                continue

            if node_kind and getattr(node, "name", None) == symbol:
                if wanted in {"any", node_kind, "method"}:
                    results.append({
                        "file": str(file_path),
                        "line": getattr(node, "lineno", 1),
                        "column": getattr(node, "col_offset", 0),
                        "name": symbol,
                        "kind": node_kind,
                    })
        return results

    def _search_jsts(self, file_path: Path, symbol: str, kind: str) -> List[Dict[str, Any]]:
        try:
            source = file_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return []
        patterns = [
            ("class", rf"^\s*export\s+class\s+{re.escape(symbol)}\b|^\s*class\s+{re.escape(symbol)}\b"),
            ("function", rf"^\s*export\s+function\s+{re.escape(symbol)}\b|^\s*function\s+{re.escape(symbol)}\b|^\s*const\s+{re.escape(symbol)}\s*=\s*(async\s*)?\("),
            ("variable", rf"^\s*(export\s+)?(const|let|var)\s+{re.escape(symbol)}\b"),
        ]
        wanted = kind.lower()
        results: List[Dict[str, Any]] = []
        for line_no, line in enumerate(source.splitlines(), 1):
            for node_kind, pattern in patterns:
                if wanted not in {"any", node_kind, "method"} and not (wanted == "function" and node_kind == "function"):
                    continue
                if re.search(pattern, line):
                    results.append({
                        "file": str(file_path),
                        "line": line_no,
                        "column": 0,
                        "name": symbol,
                        "kind": node_kind,
                    })
                    break
        return results
