"""Language Server Protocol (LSP) Client and Fallback Static Analyzer."""

from __future__ import annotations

import ast
import os
import pathlib
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlparse

LSP_SERVER_COMMANDS: dict[str, list[str]] = {
    ".py": ["pyright-langserver", "--stdio"],
    ".ts": ["typescript-language-server", "--stdio"],
    ".tsx": ["typescript-language-server", "--stdio"],
    ".js": ["typescript-language-server", "--stdio"],
    ".jsx": ["typescript-language-server", "--stdio"],
    ".go": ["gopls"],
    ".rs": ["rust-analyzer"],
    ".c": ["clangd"],
    ".cpp": ["clangd"],
}


@dataclass
class LspLocation:
    path: str
    line: int  # 1-based
    character: int  # 1-based
    end_line: int | None = None
    end_character: int | None = None
    snippet: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "path": self.path,
            "line": self.line,
            "character": self.character,
        }
        if self.end_line is not None:
            d["endLine"] = self.end_line
        if self.end_character is not None:
            d["endCharacter"] = self.end_character
        if self.snippet:
            d["snippet"] = self.snippet
        return d


@dataclass
class LspHoverResult:
    contents: str
    range: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"contents": self.contents, "range": self.range}


@dataclass
class LspSymbol:
    name: str
    kind: str
    path: str
    line: int
    character: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "path": self.path,
            "line": self.line,
            "character": self.character,
        }


def _file_to_uri(file_path: str) -> str:
    p = pathlib.Path(file_path).resolve()
    return p.as_uri()


def _uri_to_file(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        return unquote(parsed.path)
    return uri


class LspClient:
    """LSP Client connecting to local language servers over stdio, with fallback static analysis."""

    def __init__(self, workspace_root: str = ".") -> None:
        self.workspace_root = str(pathlib.Path(workspace_root).resolve())
        self._servers: dict[str, subprocess.Popen[bytes]] = {}
        self._req_id = 1
        self._lock = threading.Lock()

    def _find_server_for_ext(self, ext: str) -> list[str] | None:
        cmd = LSP_SERVER_COMMANDS.get(ext.lower())
        if not cmd:
            return None
        bin_name = cmd[0]
        if shutil.which(bin_name):
            return cmd
        # Check alternative python server
        if ext.lower() == ".py" and shutil.which("pylsp"):
            return ["pylsp"]
        return None

    def query(
        self,
        operation: str,
        file_path: str,
        line: int,
        character: int,
        project_root: str | None = None,
    ) -> dict[str, Any]:
        """Execute an LSP query (goToDefinition, findReferences, goToImplementation, hover, documentSymbol)."""
        root = project_root or self.workspace_root
        abs_path = (
            str((pathlib.Path(root) / file_path).resolve())
            if not os.path.isabs(file_path)
            else file_path
        )
        ext = pathlib.Path(abs_path).suffix.lower()

        # Check if a live LSP binary is available
        server_cmd = self._find_server_for_ext(ext)
        if server_cmd:
            try:
                res = self._query_live_lsp(server_cmd, operation, abs_path, line, character, root)
                if res and res.get("ok"):
                    return res
            except Exception:
                pass

        # Fallback static analysis engine
        return self._query_fallback(operation, abs_path, line, character, root)

    def _query_fallback(
        self,
        operation: str,
        file_path: str,
        line: int,
        character: int,
        root: str,
    ) -> dict[str, Any]:
        """Perform AST or regex-based fallback navigation when no live LSP server is found."""
        if not os.path.isfile(file_path):
            return {
                "ok": False,
                "error": f"File does not exist: {file_path}",
                "operation": operation,
            }

        try:
            content = pathlib.Path(file_path).read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return {"ok": False, "error": f"Failed to read file: {exc}", "operation": operation}

        lines = content.splitlines()
        target_line = lines[line - 1] if 0 <= line - 1 < len(lines) else ""
        symbol_name = self._extract_symbol_at_position(target_line, character)

        if operation in ("goToDefinition", "goToImplementation"):
            locations = self._fallback_find_definitions(symbol_name, file_path, root)
            return {
                "ok": True,
                "operation": operation,
                "symbol": symbol_name,
                "locations": [loc.to_dict() for loc in locations],
                "mode": "fallback_ast" if file_path.endswith(".py") else "fallback_regex",
            }
        elif operation == "findReferences":
            locations = self._fallback_find_references(symbol_name, root)
            return {
                "ok": True,
                "operation": operation,
                "symbol": symbol_name,
                "locations": [loc.to_dict() for loc in locations],
                "mode": "fallback_regex",
            }
        elif operation == "hover":
            hover = self._fallback_hover(symbol_name, file_path, lines, line)
            return {
                "ok": True,
                "operation": operation,
                "symbol": symbol_name,
                "hover": hover.to_dict() if hover else None,
                "mode": "fallback_ast",
            }
        elif operation == "documentSymbol":
            symbols = self._fallback_document_symbols(file_path, content)
            return {
                "ok": True,
                "operation": operation,
                "symbols": [sym.to_dict() for sym in symbols],
                "mode": "fallback_ast",
            }
        else:
            return {
                "ok": False,
                "error": f"Unsupported LSP operation: {operation}",
                "operation": operation,
            }

    def _extract_symbol_at_position(self, line_text: str, character: int) -> str:
        if not line_text:
            return ""
        idx = max(0, min(character - 1, len(line_text) - 1))
        # Expand word under cursor
        start = idx
        while start > 0 and (line_text[start - 1].isalnum() or line_text[start - 1] == "_"):
            start -= 1
        end = idx
        while end < len(line_text) and (line_text[end].isalnum() or line_text[end] == "_"):
            end += 1
        return line_text[start:end]

    def _fallback_find_definitions(
        self, symbol: str, current_file: str, root: str
    ) -> list[LspLocation]:
        if not symbol:
            return []
        locations: list[LspLocation] = []

        # 1. Check Python AST in current file and surrounding project files
        if current_file.endswith(".py"):
            locations.extend(self._python_ast_find_defs(symbol, current_file))

        # 2. Ripgrep search for def/class/function/const declarations
        pattern = rf"\b(def|class|function|const|let|var|type|interface|fn|func|struct|enum)\s+{re.escape(symbol)}\b"
        try:
            rg_bin = shutil.which("rg") or "rg"
            cmd = [rg_bin, "-n", "-C", "1", pattern, root]
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5.0)
            if out.returncode == 0:
                for line in out.stdout.splitlines():
                    parts = line.split(":", 2)
                    if len(parts) >= 3 and parts[1].isdigit():
                        fpath = parts[0]
                        lineno = int(parts[1])
                        snip = parts[2].strip()
                        if not any(loc.path == fpath and loc.line == lineno for loc in locations):
                            locations.append(
                                LspLocation(
                                    path=fpath,
                                    line=lineno,
                                    character=1,
                                    snippet=snip,
                                )
                            )
        except Exception:
            pass

        return locations[:50]

    def _python_ast_find_defs(self, symbol: str, file_path: str) -> list[LspLocation]:
        locs: list[LspLocation] = []
        try:
            content = pathlib.Path(file_path).read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(content, filename=file_path)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if node.name == symbol:
                        locs.append(
                            LspLocation(
                                path=file_path,
                                line=node.lineno,
                                character=node.col_offset + 1,
                                snippet=f"{'class' if isinstance(node, ast.ClassDef) else 'def'} {node.name}",
                            )
                        )
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == symbol:
                            locs.append(
                                LspLocation(
                                    path=file_path,
                                    line=target.lineno,
                                    character=target.col_offset + 1,
                                    snippet=f"{symbol} = ...",
                                )
                            )
        except Exception:
            pass
        return locs

    def _fallback_find_references(self, symbol: str, root: str) -> list[LspLocation]:
        if not symbol:
            return []
        locations: list[LspLocation] = []
        try:
            rg_bin = shutil.which("rg") or "rg"
            pattern = rf"\b{re.escape(symbol)}\b"
            cmd = [rg_bin, "-n", "--max-count", "100", pattern, root]
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5.0)
            if out.returncode == 0:
                for line in out.stdout.splitlines():
                    parts = line.split(":", 2)
                    if len(parts) >= 3 and parts[1].isdigit():
                        fpath = parts[0]
                        lineno = int(parts[1])
                        snip = parts[2].strip()
                        locations.append(
                            LspLocation(
                                path=fpath,
                                line=lineno,
                                character=1,
                                snippet=snip,
                            )
                        )
        except Exception:
            pass
        return locations[:100]

    def _fallback_hover(
        self, symbol: str, file_path: str, lines: list[str], line_num: int
    ) -> LspHoverResult | None:
        if not symbol:
            return None

        # Check Python docstring via AST
        if file_path.endswith(".py"):
            try:
                content = "\n".join(lines)
                tree = ast.parse(content)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        if node.name == symbol:
                            doc = ast.get_docstring(node) or "No docstring available."
                            args_str = ""
                            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                args_list = [a.arg for a in node.args.args]
                                args_str = f"({', '.join(args_list)})"
                            kind = "class" if isinstance(node, ast.ClassDef) else "function"
                            hover_text = f"```python\n{kind} {symbol}{args_str}\n```\n\n{doc}"
                            return LspHoverResult(contents=hover_text)
            except Exception:
                pass

        # Generic hover
        curr_line = lines[line_num - 1] if 0 <= line_num - 1 < len(lines) else ""
        return LspHoverResult(contents=f"Symbol: `{symbol}`\nContext: `{curr_line.strip()}`")

    def _fallback_document_symbols(self, file_path: str, content: str) -> list[LspSymbol]:
        symbols: list[LspSymbol] = []
        if file_path.endswith(".py"):
            try:
                tree = ast.parse(content, filename=file_path)
                for node in ast.iter_child_nodes(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        symbols.append(
                            LspSymbol(
                                name=node.name,
                                kind="function",
                                path=file_path,
                                line=node.lineno,
                                character=node.col_offset + 1,
                            )
                        )
                    elif isinstance(node, ast.ClassDef):
                        symbols.append(
                            LspSymbol(
                                name=node.name,
                                kind="class",
                                path=file_path,
                                line=node.lineno,
                                character=node.col_offset + 1,
                            )
                        )
            except Exception:
                pass
        return symbols

    def _query_live_lsp(
        self,
        cmd: list[str],
        operation: str,
        file_path: str,
        line: int,
        character: int,
        root: str,
    ) -> dict[str, Any] | None:
        # Placeholder for full stdio JSON-RPC lifecycle if server process is running
        return None


_default_lsp_client: LspClient | None = None


def get_lsp_client(workspace_root: str = ".") -> LspClient:
    global _default_lsp_client
    if _default_lsp_client is None:
        _default_lsp_client = LspClient(workspace_root=workspace_root)
    return _default_lsp_client
