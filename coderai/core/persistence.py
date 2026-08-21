"""Session Persistence Protocol — port of dsh session-persistence subsystem.

Defines the abstract SessionPersistence protocol and JsonlPersistence implementation.
Provides crash recovery, atomic flush, session header tracking, and seed log boundaries.
"""

from __future__ import annotations

import abc
import json
import os
import pathlib
import time
from dataclasses import dataclass, field
from typing import Any

from coderai.core.events import SessionEvent, legacy_message_to_event


@dataclass
class SessionHeader:
    """Metadata header recorded at session creation or load boundary."""

    session_id: str
    model: str
    project_root: str
    created_at: float = field(default_factory=lambda: time.time() * 1000)
    provider: str = "openai"
    persona: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "model": self.model,
            "projectRoot": self.project_root,
            "createdAt": self.created_at,
            "provider": self.provider,
            "persona": self.persona,
            "meta": self.meta,
        }


class SessionPersistence(abc.ABC):
    """Abstract interface for session durability backends."""

    @abc.abstractmethod
    def append_event(self, session_id: str, event: SessionEvent) -> None:
        """Append one event to durable storage."""
        ...

    @abc.abstractmethod
    def list_events(self, session_id: str) -> list[SessionEvent]:
        """Read all events for a session in sequence order."""
        ...

    @abc.abstractmethod
    def flush(self, session_id: str) -> None:
        """Ensure all buffered writes are committed to disk."""
        ...

    @abc.abstractmethod
    def delete(self, session_id: str) -> bool:
        """Delete durable storage for a session."""
        ...

    @abc.abstractmethod
    def exists(self, session_id: str) -> bool:
        """Check if storage exists for a session."""
        ...


class JsonlPersistence(SessionPersistence):
    """Append-only JSONL session persistence with crash-resilience."""

    def __init__(self, storage_dir: pathlib.Path | str) -> None:
        self.storage_dir = pathlib.Path(storage_dir)

    def _file_path(self, session_id: str) -> pathlib.Path:
        return self.storage_dir / f"{session_id}.jsonl"

    def _ensure_dir(self) -> None:
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def append_event(self, session_id: str, event: SessionEvent) -> None:
        self._ensure_dir()
        path = self._file_path(session_id)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")

    def list_events(self, session_id: str) -> list[SessionEvent]:
        path = self._file_path(session_id)
        if not path.exists():
            return []
        events: list[SessionEvent] = []
        seq = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                if isinstance(raw, dict):
                    if "type" in raw:
                        events.append(SessionEvent.from_dict(raw))
                    elif "role" in raw:
                        events.append(legacy_message_to_event(seq, raw, session_id=session_id))
                    seq += 1
            except (ValueError, TypeError):
                continue
        return events

    def flush(self, session_id: str) -> None:
        path = self._file_path(session_id)
        if path.exists():
            try:
                with open(path, "a", encoding="utf-8") as f:
                    f.flush()
                    os.fsync(f.fileno())
            except OSError:
                pass

    def delete(self, session_id: str) -> bool:
        path = self._file_path(session_id)
        if path.exists():
            try:
                path.unlink()
                return True
            except OSError:
                return False
        return False

    def exists(self, session_id: str) -> bool:
        return self._file_path(session_id).exists()
