"""Session Query SQLite Projection and Full-Text Search Engine.

Implements DeepSeek Harness session-query-sqlite architecture for durable cross-session querying.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SessionSummary:
    session_id: str
    title: str
    created_at: float
    updated_at: float
    turn_count: int
    total_tokens: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "title": self.title,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "turnCount": self.turn_count,
            "totalTokens": self.total_tokens,
        }


@dataclass
class QuerySearchResult:
    session_id: str
    event_seq: int
    role: str
    snippet: str
    timestamp: float
    tool_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "sessionId": self.session_id,
            "seq": self.event_seq,
            "role": self.role,
            "snippet": self.snippet,
            "timestamp": self.timestamp,
        }
        if self.tool_name:
            d["toolName"] = self.tool_name
        return d


class SessionSqliteIndex:
    """Manages SQLite projection database and FTS5 search index for session logs."""

    def __init__(self, db_path: str | pathlib.Path) -> None:
        self.db_path = pathlib.Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock:
            with self._get_connection() as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        session_id TEXT PRIMARY KEY,
                        title TEXT,
                        created_at REAL,
                        updated_at REAL,
                        turn_count INTEGER DEFAULT 0,
                        total_tokens INTEGER DEFAULT 0
                    );

                    CREATE TABLE IF NOT EXISTS events (
                        session_id TEXT,
                        seq INTEGER,
                        event_type TEXT,
                        role TEXT,
                        content TEXT,
                        tool_name TEXT,
                        timestamp REAL,
                        PRIMARY KEY (session_id, seq)
                    );

                    CREATE VIRTUAL TABLE IF NOT EXISTS fts_events USING fts5(
                        session_id UNINDEXED,
                        seq UNINDEXED,
                        role,
                        content,
                        tool_name,
                        tokenize = 'unicode61'
                    );
                    """
                )
                conn.commit()

    def index_session_header(
        self, session_id: str, title: str = "", created_at: float | None = None
    ) -> None:
        now = created_at or time.time()
        with self._lock:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO sessions (session_id, title, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        title = CASE WHEN excluded.title != '' THEN excluded.title ELSE sessions.title END,
                        updated_at = excluded.updated_at
                    """,
                    (session_id, title or "Session", now, now),
                )
                conn.commit()

    def index_event(
        self,
        session_id: str,
        seq: int,
        event_type: str,
        role: str = "",
        content: str = "",
        tool_name: str | None = None,
        timestamp: float | None = None,
    ) -> None:
        now = timestamp or time.time()
        with self._lock:
            with self._get_connection() as conn:
                # 1. Insert into events
                conn.execute(
                    """
                    INSERT OR REPLACE INTO events (session_id, seq, event_type, role, content, tool_name, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (session_id, seq, event_type, role, content, tool_name or "", now),
                )
                # 2. Insert into FTS
                if content:
                    conn.execute(
                        """
                        INSERT INTO fts_events (session_id, seq, role, content, tool_name)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (session_id, seq, role, content, tool_name or ""),
                    )
                # 3. Update session updated_at
                conn.execute(
                    """
                    UPDATE sessions SET updated_at = ? WHERE session_id = ?
                    """,
                    (now, session_id),
                )
                conn.commit()

    def search_events(
        self,
        query: str,
        session_id: str | None = None,
        role: str | None = None,
        limit: int = 20,
    ) -> list[QuerySearchResult]:
        if not query.strip():
            return []

        # Sanitize query for FTS5
        clean_query = " ".join(re.findall(r"\w+", query))
        if not clean_query:
            return []

        fts_match = f'"{clean_query}"' if " " in clean_query else f"{clean_query}*"

        sql = """
            SELECT e.session_id, e.seq, e.role, e.content, e.tool_name, e.timestamp,
                   snippet(fts_events, 3, '<b>', '</b>', '...', 20) as snip
            FROM fts_events f
            JOIN events e ON f.session_id = e.session_id AND f.seq = e.seq
            WHERE fts_events MATCH ?
        """
        params: list[Any] = [fts_match]

        if session_id:
            sql += " AND e.session_id = ?"
            params.append(session_id)
        if role:
            sql += " AND e.role = ?"
            params.append(role)

        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)

        results: list[QuerySearchResult] = []
        with self._lock:
            with self._get_connection() as conn:
                try:
                    rows = conn.execute(sql, params).fetchall()
                    for r in rows:
                        results.append(
                            QuerySearchResult(
                                session_id=r["session_id"],
                                event_seq=r["seq"],
                                role=r["role"],
                                snippet=r["snip"] or r["content"][:200],
                                timestamp=r["timestamp"],
                                tool_name=r["tool_name"] or None,
                            )
                        )
                except sqlite3.OperationalError:
                    # Fallback to LIKE if FTS match has syntax edge case
                    sql_like = """
                        SELECT session_id, seq, role, content, tool_name, timestamp
                        FROM events
                        WHERE content LIKE ?
                    """
                    params_like = [f"%{clean_query}%"]
                    if session_id:
                        sql_like += " AND session_id = ?"
                        params_like.append(session_id)
                    sql_like += " ORDER BY seq DESC LIMIT ?"
                    params_like.append(limit)
                    rows = conn.execute(sql_like, params_like).fetchall()
                    for r in rows:
                        results.append(
                            QuerySearchResult(
                                session_id=r["session_id"],
                                event_seq=r["seq"],
                                role=r["role"],
                                snippet=r["content"][:200],
                                timestamp=r["timestamp"],
                                tool_name=r["tool_name"] or None,
                            )
                        )
        return results

    def get_session_summary(self, session_id: str) -> SessionSummary | None:
        with self._lock:
            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
                ).fetchone()
                if not row:
                    return None
                return SessionSummary(
                    session_id=row["session_id"],
                    title=row["title"] or "Session",
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    turn_count=row["turn_count"],
                    total_tokens=row["total_tokens"],
                )

    def list_sessions(self, limit: int = 50) -> list[SessionSummary]:
        with self._lock:
            with self._get_connection() as conn:
                rows = conn.execute(
                    "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,)
                ).fetchall()
                return [
                    SessionSummary(
                        session_id=r["session_id"],
                        title=r["title"] or "Session",
                        created_at=r["created_at"],
                        updated_at=r["updated_at"],
                        turn_count=r["turn_count"],
                        total_tokens=r["total_tokens"],
                    )
                    for r in rows
                ]


_sqlite_index_instance: SessionSqliteIndex | None = None


def get_session_sqlite_index(project_root: str = ".") -> SessionSqliteIndex:
    global _sqlite_index_instance
    if _sqlite_index_instance is None:
        db_path = pathlib.Path(project_root) / ".coderai" / "session_index.db"
        _sqlite_index_instance = SessionSqliteIndex(db_path)
    return _sqlite_index_instance
