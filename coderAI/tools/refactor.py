"""Cross-file refactoring tool — rename symbols and extract code across files."""

import ast
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from .base import Tool

logger = logging.getLogger(__name__)

_IGNORE_PARTS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".tox", ".mypy_cache", ".pytest_cache"}


class RefactorParams(BaseModel):
    action: str = Field("find_references", description="Refactoring action: rename_symbol, find_references, extract_to_module")
    symbol: Optional[str] = Field(None, description="Symbol name to refactor (function, class, variable name)")
    new_name: Optional[str] = Field(None, description="New name for the symbol (required for rename_symbol)")
    path: str = Field(".", description="Directory or file to scope the refactoring (default: current project)")
    kind: str = Field("any", description="Symbol kind filter: any, function, class, variable")
    dry_run: bool = Field(False, description="Preview changes without applying them (default: false)")


class RefactorTool(Tool):
    """Cross-file refactoring tool for renaming symbols and finding references."""

    name = "refactor"
    description = (
        "Cross-file refactoring for symbol renaming and reference finding. "
        "Supports Python (AST-aware) and JavaScript/TypeScript (regex-based). "
        "Use 'rename_symbol' to rename a symbol across all files it appears in. "
        "Use 'find_references' to list all usages of a symbol. "
        "Always use dry_run=true first to preview changes before applying."
    )
    parameters_model = RefactorParams
    is_read_only = False
    requires_confirmation = True
    timeout = None
    category = "other"

    async def execute(
        self,
        action: str,
        symbol: str,
        new_name: Optional[str] = None,
        path: str = ".",
        kind: str = "any",
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        try:
            action = action.strip().lower()
            if action not in ("rename_symbol", "find_references", "extract_to_module"):
                return {
                    "success": False,
                    "error": f"Unknown action: {action}. Supported: rename_symbol, find_references, extract_to_module.",
                }

            if action == "rename_symbol" and not new_name:
                return {
                    "success": False,
                    "error": "new_name is required for rename_symbol action.",
                }

            if action == "extract_to_module":
                return {
                    "success": False,
                    "error": (
                        "extract_to_module is not yet supported. "
                        "Use find_references or rename_symbol instead."
                    ),
                }

            base = Path(path).expanduser().resolve()
            if not base.exists():
                return {"success": False, "error": f"Path not found: {path}"}

            files = self._collect_files(base)
            if not files:
                return {
                    "success": False,
                    "error": "No source files found in the specified path.",
                }

            if action == "find_references":
                all_refs = self._find_all_references(files, symbol, kind)
                return {
                    "success": True,
                    "action": "find_references",
                    "symbol": symbol,
                    "kind": kind,
                    "total_references": sum(len(r["references"]) for r in all_refs),
                    "files_with_references": len(all_refs),
                    "files": all_refs,
                }

            if action == "rename_symbol":
                all_refs = self._find_all_references(files, symbol, kind)

                if sum(len(r["references"]) for r in all_refs) == 0:
                    return {
                        "success": False,
                        "error": f"No references found for symbol '{symbol}'. Verify the symbol name and path.",
                    }

                if dry_run:
                    return {
                        "success": True,
                        "action": "rename_symbol",
                        "dry_run": True,
                        "symbol": symbol,
                        "new_name": new_name,
                        "total_changes": sum(len(r["references"]) for r in all_refs),
                        "files_affected": len(all_refs),
                        "files": [
                            {
                                "file": f["file"],
                                "changes": f["references"],
                            }
                            for f in all_refs
                        ],
                        "message": (
                            f"Dry run: would rename '{symbol}' to '{new_name}' "
                            f"in {len(all_refs)} file(s) with {sum(len(r['references']) for r in all_refs)} change(s). "
                            "Review the changes above, then run again with dry_run=false to apply."
                        ),
                    }

                modified_files = self._apply_rename(all_refs, symbol, new_name, base)
                return {
                    "success": True,
                    "action": "rename_symbol",
                    "dry_run": False,
                    "symbol": symbol,
                    "new_name": new_name,
                    "files_modified": len(modified_files),
                    "files": modified_files,
                    "message": (
                        f"Renamed '{symbol}' to '{new_name}' in {len(modified_files)} file(s)."
                    ),
                }

        except Exception as e:
            logger.exception("refactor failed")
            return {"success": False, "error": str(e)}

    def _collect_files(self, base: Path) -> List[Path]:
        files: List[Path] = []
        if base.is_file():
            if base.suffix.lower() in {".py", ".ts", ".tsx", ".js", ".jsx"}:
                return [base]
            return []

        for f in sorted(base.rglob("*")):
            if not f.is_file():
                continue
            if any(part in _IGNORE_PARTS for part in f.parts):
                continue
            if f.suffix.lower() in {".py", ".ts", ".tsx", ".js", ".jsx"}:
                files.append(f)

        return files

    def _find_all_references(self, files: List[Path], symbol: str, kind: str) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for file_path in files:
            refs = self._find_references_in_file(file_path, symbol, kind)
            if refs:
                results.append({"file": str(file_path), "references": refs})
        return results

    def _find_references_in_file(self, file_path: Path, symbol: str, kind: str) -> List[Dict[str, Any]]:
        suffix = file_path.suffix.lower()
        try:
            source = file_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return []

        if suffix == ".py":
            return self._find_python_references(source, symbol, kind)
        elif suffix in {".ts", ".tsx", ".js", ".jsx"}:
            return self._find_jsts_references(source, symbol, kind)
        return []

    def _find_python_references(self, source: str, symbol: str, kind: str) -> List[Dict[str, Any]]:
        refs: List[Dict[str, Any]] = []

        try:
            tree = ast.parse(source)
            lines = source.splitlines()
        except Exception:
            return refs

        wanted = kind.lower()

        for node in ast.walk(tree):
            line_no = 0
            col = 0
            ref_kind = ""
            ref_name = ""

            if isinstance(node, ast.FunctionDef):
                ref_kind = "function"
                if node.name == symbol:
                    ref_name = node.name
                    line_no = node.lineno
                    col = node.col_offset + 4
            elif isinstance(node, ast.ClassDef):
                ref_kind = "class"
                if node.name == symbol:
                    ref_name = node.name
                    line_no = node.lineno
                    col = node.col_offset + 6
            elif isinstance(node, ast.Name):
                if node.id == symbol:
                    ref_name = node.id
                    line_no = node.lineno if hasattr(node, "lineno") else 0
                    col = node.col_offset if hasattr(node, "col_offset") else 0
                    is_def = isinstance(node.ctx, ast.Store) if hasattr(node, "ctx") else False
                    ref_kind = "definition" if is_def else "reference"
            elif isinstance(node, ast.Attribute):
                if isinstance(node.value, ast.Attribute):
                    continue
                if getattr(node, "attr", None) == symbol:
                    ref_name = node.attr
                    line_no = node.end_lineno if hasattr(node, "end_lineno") else 0
                    col = node.end_col_offset if hasattr(node, "end_col_offset") else 0
                    ref_kind = "attribute_access"

            if ref_name and line_no > 0:
                if wanted not in {"any", ref_kind, "function", "class", "variable"}:
                    if ref_kind == "attribute_access" and wanted != "any":
                        continue
                    if ref_kind in ("definition", "reference") and wanted not in ("any", "variable"):
                        continue

                dedup_key = (line_no, col)
                if dedup_key in {(r["line"], r["column"]) for r in refs}:
                    continue

                line_text = lines[line_no - 1] if 1 <= line_no <= len(lines) else ""
                refs.append({
                    "line": line_no,
                    "column": col,
                    "kind": ref_kind,
                    "symbol": ref_name,
                    "line_content": line_text.strip()[:200],
                })

        return refs

    def _find_jsts_references(self, source: str, symbol: str, kind: str) -> List[Dict[str, Any]]:
        refs: List[Dict[str, Any]] = []
        wanted = kind.lower()
        escaped = re.escape(symbol)

        patterns: List[Tuple[str, str]] = [
            ("definition", rf"\b{symbol}\s*[:=]"),
            ("definition", rf"\b(function|class|const|let|var)\s+{escaped}\b"),
            ("definition", rf"\b(export\s+)?(function|class|const|let|var)\s+{escaped}\b"),
            ("method", rf"\b{escaped}\s*\("),
            ("reference", rf"\b{escaped}\s*="),
            ("reference", rf"\b({escaped})\b(?!\s*[:=]\s*(async|\())"),
            ("attribute", rf"\.{escaped}\b"),
        ]

        lines = source.splitlines()
        for line_no, line in enumerate(lines, 1):
            for ref_kind, pattern in patterns:
                if wanted != "any" and ref_kind != wanted and ref_kind not in ("reference", "method"):
                    if wanted in ("function", "class") and ref_kind != "definition":
                        continue
                    if wanted == "variable" and ref_kind not in ("definition", "reference"):
                        continue
                for match in re.finditer(pattern, line):
                    col = match.start()
                    dedup_key = (line_no, col)
                    if dedup_key in {(r["line"], r["column"]) for r in refs}:
                        continue
                    refs.append({
                        "line": line_no,
                        "column": col,
                        "kind": ref_kind,
                        "symbol": symbol,
                        "line_content": line.strip()[:200],
                    })
        return refs

    def _apply_rename(
        self, all_refs: List[Dict[str, Any]], symbol: str, new_name: str, base: Path
    ) -> List[Dict[str, Any]]:
        modified: List[Dict[str, Any]] = []

        for file_info in all_refs:
            file_path = Path(file_info["file"])
            if not file_path.exists():
                continue

            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            original_content = content
            lines = content.splitlines(keepends=True)

            for ref in sorted(file_info["references"], key=lambda r: (r["line"], r["column"]), reverse=True):
                line_no = ref["line"]
                if line_no < 1 or line_no > len(lines):
                    continue
                idx = line_no - 1
                col = ref["column"]
                line = lines[idx]

                before = line[:col]
                after = line[col:]

                if after.startswith(symbol):
                    if ref["kind"] == "attribute_access":
                        new_line = before + "." + new_name if before.rstrip().endswith(".") else before + new_name
                    else:
                        new_line = before + new_name + after[len(symbol):]
                    lines[idx] = new_line

            new_content = "".join(lines)
            if new_content != original_content:
                from .undo import backup_store
                try:
                    backup_store.backup_file(str(file_path), "modify")
                except Exception:
                    pass
                file_path.write_text(new_content, encoding="utf-8")
                modified.append({
                    "file": str(file_path),
                    "changes_applied": len(file_info["references"]),
                })

        return modified
