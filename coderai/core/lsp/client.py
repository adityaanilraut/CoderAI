"""Language Server Protocol (LSP) Client and Fallback Static Analyzer.

Provides live JSON-RPC stdio language server connections for Python, TypeScript/JS,
Go, Rust, C/C++ with robust static analysis fallback when binaries are absent.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import os
import pathlib
import re
import shutil
import subprocess
import threading
from typing import Any

from coderai.core.lsp.instance import LspInstance
from coderai.core.lsp.protocol import (
    LspHoverResult,
    LspLocation,
    LspSymbol,
    file_to_uri,
    uri_to_file,
)

logger = logging.getLogger(__name__)

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


class LspClient:
    """LSP Client pooling live language server instances over stdio with fallback static analysis."""

    def __init__(self, workspace_root: str = ".") -> None:
        self.workspace_root = str(pathlib.Path(workspace_root).resolve())
        self._instances: dict[str, LspInstance] = {}
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

    def _get_or_create_instance(self, ext: str) -> LspInstance | None:
        cmd = self._find_server_for_ext(ext)
        if not cmd:
            return None

        key = f"{ext}:{self.workspace_root}"
        with self._lock:
            inst = self._instances.get(key)
            if inst is None or not inst.is_alive():
                inst = LspInstance(command=cmd, workspace_root=self.workspace_root)
                self._instances[key] = inst
            return inst

    def query(
        self,
        operation: str,
        file_path: str,
        line: int,
        character: int,
        project_root: str | None = None,
        timeout_s: float = 15.0,
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
        instance = self._get_or_create_instance(ext)
        if instance:
            try:
                # Run query in event loop
                res = self._run_async(
                    instance.query(
                        operation=operation,
                        file_path=abs_path,
                        line=line,
                        character=character,
                        timeout_s=timeout_s,
                    )
                )
                if res and res.get("ok"):
                    return res
            except Exception as exc:
                logger.debug(f"Live LSP query error: {exc}")

        # Fallback static analysis engine
        return self._query_fallback(operation, abs_path, line, character, root)

    def _run_async(self, coro: Any) -> Any:
        """Run a coroutine from synchronous caller safely."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # If called from an active event loop, run in a separate thread runner
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
            return fut.result(timeout=30.0)
        else:
            return asyncio.run(coro)

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
        elif operation == "workspaceSymbol":
            symbols = self._fallback_workspace_symbols(file_path, root)
            return {
                "ok": True,
                "operation": operation,
                "symbols": [sym.to_dict() for sym in symbols],
                "mode": "fallback_regex",
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

        if current_file.endswith(".py"):
            locations.extend(self._python_ast_find_defs(symbol, current_file))

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

        curr_line = lines[line_num - 1] if 0 <= line_num - 1 < len(lines) else ""
        return LspHoverResult(contents=f"Symbol: `{symbol}`\nContext: `{curr_line.strip()}`")

    def _fallback_document_symbols(self, file_path: str, content: str) -> list[LspSymbol]:
        symbols: list[LspSymbol] = []
        if file_path.endswith(".py"):
            try:
                tree = ast.parse(content, filename=file_path)
                for node in ast.walk(tree):
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

    def _fallback_workspace_symbols(self, query_str: str, root: str) -> list[LspSymbol]:
        symbols: list[LspSymbol] = []
        if not query_str:
            return symbols
        try:
            rg_bin = shutil.which("rg") or "rg"
            pattern = rf"\b(def|class|function)\s+([a-zA-Z0-9_]*{re.escape(query_str)}[a-zA-Z0-9_]*)\b"
            cmd = [rg_bin, "-n", pattern, root]
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5.0)
            if out.returncode == 0:
                for line in out.stdout.splitlines():
                    parts = line.split(":", 2)
                    if len(parts) >= 3 and parts[1].isdigit():
                        symbols.append(
                            LspSymbol(
                                name=query_str,
                                kind="symbol",
                                path=parts[0],
                                line=int(parts[1]),
                                character=1,
                            )
                        )
        except Exception:
            pass
        return symbols[:50]

    def close(self) -> None:
        """Close all running language server instances."""
        with self._lock:
            instances = list(self._instances.values())
            self._instances.clear()

        for inst in instances:
            try:
                self._run_async(inst.close())
            except Exception:
                pass


_default_lsp_client: LspClient | None = None


def get_lsp_client(workspace_root: str = ".") -> LspClient:
    global _default_lsp_client
    if _default_lsp_client is None:
        _default_lsp_client = LspClient(workspace_root=workspace_root)
    return _default_lsp_client
