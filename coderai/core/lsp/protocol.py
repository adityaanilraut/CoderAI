"""LSP Wire Protocol, Framing, and Type Definitions.

Provides Content-Length header framing, JSON-RPC 2.0 wire serialization/deserialization,
coordinate translations (1-based model coordinates to 0-based UTF-16 wire positions),
and normalized LSP data structures.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlparse
import pathlib


@dataclass(frozen=True)
class WirePosition:
    """Zero-based UTF-16 position on the wire."""

    line: int
    character: int

    def to_dict(self) -> dict[str, int]:
        return {"line": self.line, "character": self.character}


@dataclass(frozen=True)
class WireRange:
    """LSP wire range."""

    start: WirePosition
    end: WirePosition

    def to_dict(self) -> dict[str, Any]:
        return {"start": self.start.to_dict(), "end": self.end.to_dict()}


@dataclass
class LspLocation:
    """Normalized 1-based location for model and tool consumers."""

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
    """Normalized hover result."""

    contents: str
    range: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"contents": self.contents, "range": self.range}


@dataclass
class LspSymbol:
    """Normalized symbol result."""

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


def file_to_uri(file_path: str) -> str:
    """Convert absolute or relative path to file:// URI."""
    p = pathlib.Path(file_path).resolve()
    return p.as_uri()


def uri_to_file(uri: str) -> str:
    """Convert file:// URI to local file path."""
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        return unquote(parsed.path)
    return uri


def encode_lsp_message(payload: dict[str, Any]) -> bytes:
    """Encode a JSON-RPC payload with Content-Length header framing."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


class LspFrameParser:
    """Streaming parser for LSP Content-Length framed stdio streams."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        """Append incoming bytes and return any fully assembled JSON-RPC messages."""
        self._buffer.extend(data)
        messages: list[dict[str, Any]] = []

        while True:
            # Find the header/body separator \r\n\r\n
            header_end = self._buffer.find(b"\r\n\r\n")
            if header_end == -1:
                break

            header_bytes = self._buffer[:header_end]
            headers_str = header_bytes.decode("latin-1", errors="replace")

            # Extract Content-Length
            content_length = None
            for line in headers_str.split("\r\n"):
                if line.lower().startswith("content-length:"):
                    try:
                        content_length = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        pass
                    break

            if content_length is None:
                # Malformed header, skip separator and advance
                del self._buffer[: header_end + 4]
                continue

            body_start = header_end + 4
            body_end = body_start + content_length

            if len(self._buffer) < body_end:
                # Incomplete body, wait for more data
                break

            body_bytes = bytes(self._buffer[body_start:body_end])
            del self._buffer[:body_end]

            try:
                msg = json.loads(body_bytes.decode("utf-8", errors="replace"))
                if isinstance(msg, dict):
                    messages.append(msg)
            except Exception:
                continue

        return messages


def map_lsp_operation_method(operation: str) -> str:
    """Map public LSP operation to standard textDocument/* method name."""
    mapping = {
        "goToDefinition": "textDocument/definition",
        "findReferences": "textDocument/references",
        "goToImplementation": "textDocument/implementation",
        "hover": "textDocument/hover",
        "documentSymbol": "textDocument/documentSymbol",
        "workspaceSymbol": "workspace/symbol",
    }
    if operation not in mapping:
        raise ValueError(f"Unsupported LSP operation: {operation}")
    return mapping[operation]


def normalize_wire_location(raw_loc: dict[str, Any], project_root: str = "") -> LspLocation | None:
    """Normalize Location or LocationLink payload into LspLocation."""
    if not isinstance(raw_loc, dict):
        return None

    # Handle LocationLink (targetUri + targetSelectionRange or targetRange)
    if "targetUri" in raw_loc:
        uri = str(raw_loc.get("targetUri", ""))
        range_obj = raw_loc.get("targetSelectionRange") or raw_loc.get("targetRange") or {}
    else:
        # Handle standard Location (uri + range)
        uri = str(raw_loc.get("uri", ""))
        range_obj = raw_loc.get("range") or {}

    if not uri:
        return None

    file_path = uri_to_file(uri)
    if project_root:
        try:
            rel = str(pathlib.Path(file_path).relative_to(project_root))
            file_path = rel
        except ValueError:
            pass

    start = range_obj.get("start", {})
    end = range_obj.get("end", {})

    line = int(start.get("line", 0)) + 1  # 0-based to 1-based
    character = int(start.get("character", 0)) + 1

    end_line = int(end.get("line", 0)) + 1 if "line" in end else None
    end_character = int(end.get("character", 0)) + 1 if "character" in end else None

    # Try to extract snippet from file if available
    snippet = None
    try:
        abs_p = pathlib.Path(project_root, file_path) if project_root else pathlib.Path(file_path)
        if abs_p.is_file():
            lines = abs_p.read_text(encoding="utf-8", errors="replace").splitlines()
            if 0 <= line - 1 < len(lines):
                snippet = lines[line - 1].strip()
    except Exception:
        pass

    return LspLocation(
        path=file_path,
        line=line,
        character=character,
        end_line=end_line,
        end_character=end_character,
        snippet=snippet,
    )


def normalize_wire_hover(raw_hover: dict[str, Any]) -> LspHoverResult | None:
    """Normalize LSP Hover payload into markdown/plaintext content."""
    if not isinstance(raw_hover, dict):
        return None

    contents = raw_hover.get("contents")
    if not contents:
        return None

    extracted_text = ""
    if isinstance(contents, str):
        extracted_text = contents
    elif isinstance(contents, dict):
        # MarkupContent: { kind: 'markdown' | 'plaintext', value: '...' }
        extracted_text = str(contents.get("value", ""))
    elif isinstance(contents, list):
        # Array of MarkedString
        parts = []
        for item in contents:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                val = str(item.get("value", ""))
                lang = str(item.get("language", ""))
                if lang and val:
                    parts.append(f"```{lang}\n{val}\n```")
                elif val:
                    parts.append(val)
        extracted_text = "\n\n".join(parts)

    if not extracted_text.strip():
        return None

    return LspHoverResult(contents=extracted_text.strip(), range=raw_hover.get("range"))
