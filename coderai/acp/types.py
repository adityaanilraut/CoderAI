"""Agent Control Protocol (ACP) — Protocol types and ndjson streaming codec."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import acp

PROTOCOL_VERSION = "0.25.1"

# ACP SDK schema aliases used by acp/mcp.py.
MCPServer = acp.schema.HttpMcpServer | acp.schema.SseMcpServer | acp.schema.McpServerStdio

ACPContentBlock = (
    acp.schema.TextContentBlock
    | acp.schema.ImageContentBlock
    | acp.schema.AudioContentBlock
    | acp.schema.ResourceContentBlock
    | acp.schema.EmbeddedResourceContentBlock
)


@dataclass
class AcpMessage:
    """A single JSON-RPC 2.0 or ACP ndjson message."""

    jsonrpc: str = "2.0"
    id: str | int | None = None
    method: str | None = None
    params: dict[str, Any] | None = None
    result: Any = None
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"jsonrpc": self.jsonrpc}
        if self.id is not None:
            d["id"] = self.id
        if self.method is not None:
            d["method"] = self.method
        if self.params is not None:
            d["params"] = self.params
        if self.result is not None:
            d["result"] = self.result
        if self.error is not None:
            d["error"] = self.error
        return d

    def encode_ndjson(self) -> bytes:
        return (json.dumps(self.to_dict(), ensure_ascii=False) + "\n").encode("utf-8")
