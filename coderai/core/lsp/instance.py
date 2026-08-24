"""LSP Instance — manages the initialized server lifecycle, capabilities, and document sync."""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
from typing import Any

from coderai.core.lsp.connection import LspConnection
from coderai.core.lsp.protocol import (
    LspLocation,
    LspSymbol,
    file_to_uri,
    map_lsp_operation_method,
    normalize_wire_hover,
    normalize_wire_location,
)

logger = logging.getLogger(__name__)


class LspInstance:
    """A single initialized LSP server process with capability negotiation and document tracking."""

    def __init__(
        self,
        command: list[str],
        workspace_root: str,
        initialization_options: dict[str, Any] | None = None,
    ) -> None:
        self.command = command
        self.workspace_root = str(pathlib.Path(workspace_root).resolve())
        self.workspace_uri = file_to_uri(self.workspace_root)
        self.initialization_options = initialization_options or {}
        self.connection = LspConnection(command=command, cwd=self.workspace_root)
        self.server_capabilities: dict[str, Any] = {}
        self._initialized = False
        self._opened_files: set[str] = set()
        self._lock = asyncio.Lock()

    def is_alive(self) -> bool:
        return self.connection.is_alive()

    async def start_and_initialize(self, timeout_s: float = 15.0) -> bool:
        """Start the server subprocess and complete the initialize handshake."""
        async with self._lock:
            if self._initialized and self.is_alive():
                return True

            self.connection.start()

            client_capabilities = {
                "workspace": {
                    "applyEdit": True,
                    "workspaceEdit": {"documentChanges": True},
                    "symbol": {"dynamicRegistration": False},
                },
                "textDocument": {
                    "synchronization": {
                        "dynamicRegistration": False,
                        "willSave": False,
                        "willSaveWaitUntil": False,
                        "didSave": True,
                    },
                    "hover": {
                        "dynamicRegistration": False,
                        "contentFormat": ["markdown", "plaintext"],
                    },
                    "definition": {"dynamicRegistration": False, "linkSupport": True},
                    "references": {"dynamicRegistration": False},
                    "implementation": {"dynamicRegistration": False, "linkSupport": True},
                    "documentSymbol": {
                        "dynamicRegistration": False,
                        "hierarchicalDocumentSymbolSupport": True,
                    },
                },
            }

            init_params = {
                "processId": os.getpid(),
                "rootUri": self.workspace_uri,
                "rootPath": self.workspace_root,
                "workspaceFolders": [
                    {
                        "name": pathlib.Path(self.workspace_root).name or "workspace",
                        "uri": self.workspace_uri,
                    }
                ],
                "capabilities": client_capabilities,
                "initializationOptions": self.initialization_options,
            }

            try:
                res = await self.connection.send_request(
                    "initialize", init_params, timeout_s=timeout_s
                )
                if isinstance(res, dict):
                    self.server_capabilities = res.get("capabilities", {})
                self.connection.send_notification("initialized", {})
                self._initialized = True
                return True
            except Exception as exc:
                logger.debug(f"Failed to initialize LSP server '{self.command[0]}': {exc}")
                await self.close()
                return False

    def ensure_document_opened(self, file_path: str, content: str | None = None) -> None:
        """Send textDocument/didOpen if not already opened in this session."""
        abs_path = (
            str(pathlib.Path(self.workspace_root, file_path).resolve())
            if not os.path.isabs(file_path)
            else file_path
        )
        if abs_path in self._opened_files:
            return

        uri = file_to_uri(abs_path)
        ext = pathlib.Path(abs_path).suffix.lower()
        lang_id = {
            ".py": "python",
            ".ts": "typescript",
            ".tsx": "typescriptreact",
            ".js": "javascript",
            ".jsx": "javascriptreact",
            ".go": "go",
            ".rs": "rust",
            ".c": "c",
            ".cpp": "cpp",
            ".json": "json",
            ".md": "markdown",
        }.get(ext, "plaintext")

        if content is None:
            try:
                content = pathlib.Path(abs_path).read_text(encoding="utf-8", errors="replace")
            except Exception:
                content = ""

        self.connection.send_notification(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": lang_id,
                    "version": 1,
                    "text": content,
                }
            },
        )
        self._opened_files.add(abs_path)

    async def query(
        self,
        operation: str,
        file_path: str,
        line: int,  # 1-based
        character: int,  # 1-based
        timeout_s: float = 15.0,
    ) -> dict[str, Any]:
        """Execute a structured LSP operation against the live server."""
        if not self._initialized or not self.is_alive():
            ok = await self.start_and_initialize()
            if not ok:
                return {"ok": False, "error": f"LSP server for {file_path} failed to initialize"}

        abs_path = (
            str(pathlib.Path(self.workspace_root, file_path).resolve())
            if not os.path.isabs(file_path)
            else file_path
        )
        uri = file_to_uri(abs_path)
        self.ensure_document_opened(abs_path)

        # Convert 1-based coordinates to 0-based UTF-16 wire positions
        wire_line = max(0, line - 1)
        wire_char = max(0, character - 1)
        wire_position = {"line": wire_line, "character": wire_char}

        method = map_lsp_operation_method(operation)
        params: dict[str, Any]

        if operation in ("goToDefinition", "goToImplementation"):
            params = {"textDocument": {"uri": uri}, "position": wire_position}
        elif operation == "findReferences":
            params = {
                "textDocument": {"uri": uri},
                "position": wire_position,
                "context": {"includeDeclaration": True},
            }
        elif operation == "hover":
            params = {"textDocument": {"uri": uri}, "position": wire_position}
        elif operation == "documentSymbol":
            params = {"textDocument": {"uri": uri}}
        elif operation == "workspaceSymbol":
            params = {"query": file_path}
        else:
            return {"ok": False, "error": f"Unsupported LSP operation: {operation}"}

        try:
            raw_result = await self.connection.send_request(method, params, timeout_s=timeout_s)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "operation": operation}

        # Normalize outputs
        if operation in ("goToDefinition", "goToImplementation", "findReferences"):
            locations: list[LspLocation] = []
            if isinstance(raw_result, list):
                for item in raw_result:
                    norm = normalize_wire_location(item, project_root=self.workspace_root)
                    if norm:
                        locations.append(norm)
            elif isinstance(raw_result, dict):
                norm = normalize_wire_location(raw_result, project_root=self.workspace_root)
                if norm:
                    locations.append(norm)
            return {
                "ok": True,
                "operation": operation,
                "locations": [loc.to_dict() for loc in locations],
                "mode": "live_lsp",
            }
        elif operation == "hover":
            hover = normalize_wire_hover(raw_result) if isinstance(raw_result, dict) else None
            return {
                "ok": True,
                "operation": operation,
                "hover": hover.to_dict() if hover else None,
                "mode": "live_lsp",
            }
        elif operation == "documentSymbol":
            symbols: list[LspSymbol] = []
            if isinstance(raw_result, list):
                for item in raw_result:
                    name = item.get("name", "")
                    kind = str(item.get("kind", ""))
                    loc = item.get("location") or {}
                    norm_loc = normalize_wire_location(loc, project_root=self.workspace_root)
                    symbols.append(
                        LspSymbol(
                            name=name,
                            kind=kind,
                            path=norm_loc.path if norm_loc else file_path,
                            line=norm_loc.line if norm_loc else 1,
                            character=norm_loc.character if norm_loc else 1,
                        )
                    )
            return {
                "ok": True,
                "operation": operation,
                "symbols": [s.to_dict() for s in symbols],
                "mode": "live_lsp",
            }

        return {"ok": True, "operation": operation, "result": raw_result, "mode": "live_lsp"}

    async def close(self) -> None:
        """Close the server and release opened document references."""
        async with self._lock:
            if not self._initialized and not self.connection.is_alive():
                return
            try:
                if self.is_alive():
                    await self.connection.send_request("shutdown", {}, timeout_s=2.0)
            except Exception:
                pass
            await self.connection.close()
            self._opened_files.clear()
            self._initialized = False
