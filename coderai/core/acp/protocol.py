"""Agent Control Protocol (ACP) — Protocol types and ndjson streaming codec."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = "0.25.1"


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


class AcpNdjsonParser:
    """Streaming line-based NDJSON parser for ACP child process output."""

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, data: str | bytes) -> list[AcpMessage]:
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")
        self._buffer += data

        messages: list[AcpMessage] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                if isinstance(raw, dict):
                    messages.append(
                        AcpMessage(
                            jsonrpc=raw.get("jsonrpc", "2.0"),
                            id=raw.get("id"),
                            method=raw.get("method"),
                            params=raw.get("params"),
                            result=raw.get("result"),
                            error=raw.get("error"),
                        )
                    )
            except Exception:
                continue

        return messages
