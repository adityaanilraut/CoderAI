# Ported from coderai/core/wire/store.py - kimi structure (kimi_cli/wire/file.py).
"""wire.jsonl persistence + replay (Kimi ``wire/file.py`` parity).

Format: first line ``{"type": "metadata", "protocol_version": "1.10"}``,
then one ``{"timestamp": ..., "message": {"type": ..., "payload": ...}}``
record per line. Sync API (async callers use ``asyncio.to_thread``).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Literal

from coderai.wire.protocol import WIRE_PROTOCOL_LEGACY_VERSION, WIRE_PROTOCOL_VERSION
from coderai.wire.types import WireMessageEnvelope, deserialize_wire_message


@dataclass
class WireFileMetadata:
    type: Literal["metadata"] = "metadata"
    protocol_version: str = WIRE_PROTOCOL_VERSION


@dataclass
class WireMessageRecord:
    timestamp: float
    message: WireMessageEnvelope

    @classmethod
    def from_wire_message(cls, msg: Any, *, timestamp: float) -> WireMessageRecord:
        return cls(timestamp=timestamp, message=WireMessageEnvelope.from_wire_message(msg))

    def to_wire_message(self) -> Any:
        return self.message.to_wire_message()


def parse_wire_file_metadata(line: str) -> WireFileMetadata | None:
    try:
        data = json.loads(line)
    except (ValueError, TypeError):
        return None
    if isinstance(data, dict) and data.get("type") == "metadata":
        return WireFileMetadata(protocol_version=str(data.get("protocol_version", "")))
    return None


def parse_wire_file_line(line: str) -> WireFileMetadata | WireMessageRecord:
    metadata = parse_wire_file_metadata(line)
    if metadata is not None:
        return metadata
    data = json.loads(line)
    if not isinstance(data, dict) or "message" not in data:
        raise ValueError("invalid wire record line")
    msg_data = data["message"]
    envelope = WireMessageEnvelope(
        type=str(msg_data.get("type", "")), payload=dict(msg_data.get("payload", {}))
    )
    return WireMessageRecord(timestamp=float(data.get("timestamp", 0.0)), message=envelope)


@dataclass
class WireFile:
    path: Path
    protocol_version: str = WIRE_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.path.exists():
            version = _load_protocol_version(self.path)
            self.protocol_version = (
                version if version is not None else WIRE_PROTOCOL_LEGACY_VERSION
            )
        else:
            self.protocol_version = WIRE_PROTOCOL_VERSION

    def __str__(self) -> str:
        return str(self.path)

    @property
    def version(self) -> str:
        return self.protocol_version

    def is_empty(self) -> bool:
        if not self.path.exists():
            return True
        try:
            with self.path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    if parse_wire_file_metadata(line) is not None:
                        continue
                    return False
        except OSError:
            return False
        return True

    def iter_records_sync(self) -> Iterator[WireMessageRecord]:
        if not self.path.exists():
            return
        try:
            with self.path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        parsed = parse_wire_file_line(line)
                    except Exception:
                        continue
                    if isinstance(parsed, WireFileMetadata):
                        continue
                    yield parsed
        except OSError:
            return

    async def iter_records(self) -> Any:
        for rec in self.iter_records_sync():
            yield rec

    def append_record_sync(self, record: WireMessageRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        needs_header = not self.path.exists() or self.path.stat().st_size == 0
        with self.path.open("a", encoding="utf-8") as f:
            if needs_header:
                f.write(
                    json.dumps(
                        {"type": "metadata", "protocol_version": self.protocol_version},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            f.write(
                json.dumps(
                    {
                        "timestamp": record.timestamp,
                        "message": {
                            "type": record.message.type,
                            "payload": record.message.payload,
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    def append_message_sync(self, msg: Any, *, timestamp: float | None = None) -> None:
        record = WireMessageRecord.from_wire_message(
            msg, timestamp=time.time() if timestamp is None else timestamp
        )
        self.append_record_sync(record)

    async def append_message(self, msg: Any, *, timestamp: float | None = None) -> None:
        import asyncio

        await asyncio.to_thread(self.append_message_sync, msg, timestamp=timestamp)

    async def append_record(self, record: WireMessageRecord) -> None:
        import asyncio

        await asyncio.to_thread(self.append_record_sync, record)

    def replay_sync(self) -> list[Any]:
        """Re-read all records as wire messages (print --replay parity)."""
        out: list[Any] = []
        for rec in self.iter_records_sync():
            try:
                out.append(rec.to_wire_message())
            except Exception:
                continue
        return out


def _load_protocol_version(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                metadata = parse_wire_file_metadata(line)
                if metadata is None:
                    return None
                return metadata.protocol_version
    except OSError:
        pass
    return None


def dump_line(record: WireMessageRecord) -> str:
    return (
        json.dumps(
            {
                "timestamp": record.timestamp,
                "message": {"type": record.message.type, "payload": record.message.payload},
            },
            ensure_ascii=False,
        )
        + "\n"
    )
