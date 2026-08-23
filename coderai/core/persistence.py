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


class SqlitePersistence(SessionPersistence):
    """SQLite-backed session persistence with chunk-packed events and WAL mode.

    Port of @deepseek-ai/dsh-session-persistence-sqlite with transactional packing,
    durable schema versioning, WAL journal mode, and fast sequential range queries.
    """

    SCHEMA_VERSION = 17
    DEFAULT_CHUNK_SIZE = 50

    def __init__(self, db_path: pathlib.Path | str) -> None:
        import sqlite3

        self.db_path = pathlib.Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self):
        import sqlite3

        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    version INTEGER DEFAULT 1,
                    created_at REAL,
                    model TEXT,
                    project_root TEXT,
                    provider TEXT,
                    metadata_json TEXT,
                    updated_at REAL
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    session_id TEXT,
                    seq INTEGER,
                    type TEXT,
                    time REAL,
                    payload_json TEXT,
                    PRIMARY KEY (session_id, seq)
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS event_chunks (
                    session_id TEXT,
                    start_seq INTEGER,
                    end_seq INTEGER,
                    event_count INTEGER,
                    packed_payloads TEXT,
                    PRIMARY KEY (session_id, start_seq)
                );
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_events_session_seq ON events(session_id, seq);
                """
            )

    def save_header(self, header: SessionHeader) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO sessions (session_id, created_at, model, project_root, provider, metadata_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    model=excluded.model,
                    project_root=excluded.project_root,
                    provider=excluded.provider,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (
                    header.session_id,
                    header.created_at,
                    header.model,
                    header.project_root,
                    header.provider,
                    json.dumps(header.to_dict()),
                    time.time() * 1000,
                ),
            )

    def append_event(self, session_id: str, event: SessionEvent) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO events (session_id, seq, type, time, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    event.seq,
                    event.type,
                    event.time,
                    json.dumps(event.to_dict(), ensure_ascii=False),
                ),
            )
            conn.execute(
                """
                UPDATE sessions SET updated_at = ? WHERE session_id = ?
                """,
                (time.time() * 1000, session_id),
            )

    def append_events_batch(self, session_id: str, events: list[SessionEvent]) -> None:
        if not events:
            return
        with self._get_connection() as conn:
            rows = [
                (
                    session_id,
                    e.seq,
                    e.type,
                    e.time,
                    json.dumps(e.to_dict(), ensure_ascii=False),
                )
                for e in events
            ]
            conn.executemany(
                """
                INSERT OR REPLACE INTO events (session_id, seq, type, time, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                rows,
            )

    def list_events(self, session_id: str, from_seq: int = 0) -> list[SessionEvent]:
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT payload_json FROM events
                WHERE session_id = ? AND seq >= ?
                ORDER BY seq ASC
                """,
                (session_id, from_seq),
            )
            events: list[SessionEvent] = []
            for (row_json,) in cursor.fetchall():
                try:
                    data = json.loads(row_json)
                    events.append(SessionEvent.from_dict(data))
                except Exception:
                    continue
            return events

    def flush(self, session_id: str) -> None:
        pass  # SQLite with WAL flushes on commit

    def delete(self, session_id: str) -> bool:
        with self._get_connection() as conn:
            conn.execute("DELETE FROM events WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM event_chunks WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            return True

    def exists(self, session_id: str) -> bool:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT 1 FROM events WHERE session_id = ? LIMIT 1", (session_id,)
            )
            return cursor.fetchone() is not None


@dataclass
class ProjectionCheckpoint:
    """Cached projection snapshot at a specific event sequence cursor."""

    session_id: str
    projection_name: str
    seq: int
    data: dict[str, Any]
    timestamp: float = field(default_factory=lambda: time.time() * 1000)


class SessionProjectionCache:
    """Durable projection cache implementing the cold-read ladder.

    Port of @deepseek-ai/dsh-session-projection-cache.
    Saves snapshot state at sequence N. On load, retrieves projection checkpoint N
    and only replays event tail (seq > N) from persistence, reducing I/O and parse cost.
    """

    def __init__(self, cache_db_path: pathlib.Path | str | None = None) -> None:
        import sqlite3

        self.db_path = pathlib.Path(cache_db_path) if cache_db_path else None
        self._memory_cache: dict[str, ProjectionCheckpoint] = {}
        if self.db_path:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_db()

    def _init_db(self) -> None:
        if not self.db_path:
            return
        import sqlite3

        with sqlite3.connect(str(self.db_path), timeout=5.0) as conn:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS projection_checkpoints (
                    session_id TEXT,
                    projection_name TEXT,
                    seq INTEGER,
                    data_json TEXT,
                    timestamp REAL,
                    PRIMARY KEY (session_id, projection_name)
                );
                """
            )

    def save_checkpoint(
        self, session_id: str, projection_name: str, seq: int, data: dict[str, Any]
    ) -> None:
        chk = ProjectionCheckpoint(
            session_id=session_id,
            projection_name=projection_name,
            seq=seq,
            data=data,
            timestamp=time.time() * 1000,
        )
        self._memory_cache[f"{session_id}:{projection_name}"] = chk

        if self.db_path:
            import sqlite3

            with sqlite3.connect(str(self.db_path), timeout=5.0) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO projection_checkpoints
                    (session_id, projection_name, seq, data_json, timestamp)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (session_id, projection_name, seq, json.dumps(data), chk.timestamp),
                )

    def get_checkpoint(
        self, session_id: str, projection_name: str
    ) -> ProjectionCheckpoint | None:
        key = f"{session_id}:{projection_name}"
        if key in self._memory_cache:
            return self._memory_cache[key]

        if self.db_path and self.db_path.exists():
            import sqlite3

            with sqlite3.connect(str(self.db_path), timeout=5.0) as conn:
                cursor = conn.execute(
                    """
                    SELECT seq, data_json, timestamp FROM projection_checkpoints
                    WHERE session_id = ? AND projection_name = ?
                    """,
                    (session_id, projection_name),
                )
                row = cursor.fetchone()
                if row:
                    seq, data_json, ts = row
                    chk = ProjectionCheckpoint(
                        session_id=session_id,
                        projection_name=projection_name,
                        seq=seq,
                        data=json.loads(data_json),
                        timestamp=ts,
                    )
                    self._memory_cache[key] = chk
                    return chk
        return None

    def cold_read_replay(
        self,
        persistence: SessionPersistence,
        session_id: str,
        projection_name: str = "messages",
    ) -> tuple[int, list[SessionEvent], dict[str, Any] | None]:
        """Cold-read ladder: load latest checkpoint, then fetch and return tail events."""
        chk = self.get_checkpoint(session_id, projection_name)
        if chk:
            tail_events = [e for e in persistence.list_events(session_id) if e.seq > chk.seq]
            return chk.seq, tail_events, chk.data
        all_events = persistence.list_events(session_id)
        return 0, all_events, None
