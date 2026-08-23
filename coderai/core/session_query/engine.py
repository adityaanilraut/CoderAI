"""Unified Session Query Engine combining SQLite persistent index and live corpus."""

from __future__ import annotations

import logging
import os
import pathlib
from typing import Any

from coderai.core.session_query.sqlite_index import (
    QuerySearchResult,
    SessionSqliteIndex,
    SessionSummary,
    get_session_sqlite_index,
)

logger = logging.getLogger(__name__)


class SessionQueryEngine:
    """Unified engine to query and search session history across local workspace runs."""

    def __init__(self, project_root: str = ".") -> None:
        self.project_root = project_root
        self.sqlite_index = get_session_sqlite_index(project_root)

    def search_events(
        self,
        query: str,
        session_id: str | None = None,
        role: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        results = self.sqlite_index.search_events(
            query=query,
            session_id=session_id,
            role=role,
            limit=limit,
        )
        return [r.to_dict() for r in results]

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        summaries = self.sqlite_index.list_sessions(limit=limit)
        return [s.to_dict() for s in summaries]

    def get_session_summary(self, session_id: str) -> dict[str, Any] | None:
        summary = self.sqlite_index.get_session_summary(session_id)
        return summary.to_dict() if summary else None
