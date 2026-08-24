"""Session query engine over the canonical project JSONL session store."""

from __future__ import annotations

import datetime
import json
import pathlib
import re
from typing import Any

from coderai.core.events import (
    ASSISTANT_MESSAGE,
    COMPACTION_SUMMARY,
    STEERING_MESSAGE,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
)
from coderai.core.session_store import JsonlSessionStore


def _timestamp(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
        except ValueError:
            return 0.0
    return 0.0


def _event_content(row: dict[str, Any]) -> tuple[str, str, str | None]:
    event_type = row.get("type")
    if isinstance(event_type, str):
        data = row.get("data")
        payload = data if isinstance(data, dict) else {}
        role = {
            USER_MESSAGE: "user",
            STEERING_MESSAGE: "user",
            ASSISTANT_MESSAGE: "assistant",
            TOOL_CALL: "assistant",
            TOOL_RESULT: "tool",
            COMPACTION_SUMMARY: "system",
        }.get(event_type, "")
        content = payload.get("content")
        if not isinstance(content, str):
            content = json.dumps(payload, ensure_ascii=False) if payload else ""
        tool_name = payload.get("name") or payload.get("toolName")
        return role, content, tool_name if isinstance(tool_name, str) else None

    role = str(row.get("role") or "")
    content = row.get("content")
    tool_name = row.get("name") or row.get("tool_name")
    return (
        role,
        content if isinstance(content, str) else "",
        tool_name if isinstance(tool_name, str) else None,
    )


def _snippet(content: str, query_terms: list[str], max_chars: int = 250) -> str:
    compact = " ".join(content.split())
    if len(compact) <= max_chars:
        return compact
    lower = compact.lower()
    positions = [lower.find(term) for term in query_terms]
    matched = [position for position in positions if position >= 0]
    start = max(0, (min(matched) if matched else 0) - 60)
    end = min(len(compact), start + max_chars)
    return f"{'...' if start else ''}{compact[start:end]}{'...' if end < len(compact) else ''}"


class SessionQueryEngine:
    """Query the same JSONL files used by ``JsonlSessionStore``."""

    def __init__(self, project_root: str = ".") -> None:
        self.project_root = str(pathlib.Path(project_root).resolve())
        self.store = JsonlSessionStore(self.project_root)

    def _session_files(self, session_id: str | None = None) -> list[pathlib.Path]:
        if session_id:
            candidate = self.store.messages_path(session_id)
            return [candidate] if candidate.is_file() else []
        return sorted(self.store.project_dir.glob("*.jsonl"))

    def search_events(
        self,
        query: str,
        session_id: str | None = None,
        role: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        terms = re.findall(r"\w+", query.lower())
        if not terms:
            return []

        hits: list[tuple[int, float, dict[str, Any]]] = []
        for path in self._session_files(session_id):
            for fallback_seq, row in enumerate(self.store.read_rows(path.stem)):
                row_role, content, tool_name = _event_content(row)
                if role and row_role != role:
                    continue
                lowered = content.lower()
                score = sum(lowered.count(term) for term in terms)
                if score == 0:
                    continue
                timestamp = _timestamp(
                    row.get("time")
                    or row.get("timestamp")
                    or row.get("createTime")
                    or row.get("created_at")
                )
                hit: dict[str, Any] = {
                    "sessionId": path.stem,
                    "seq": row.get("seq", fallback_seq),
                    "role": row_role,
                    "snippet": _snippet(content, terms),
                    "timestamp": timestamp,
                    "score": float(score),
                }
                if tool_name:
                    hit["toolName"] = tool_name
                hits.append((score, timestamp, hit))
        hits.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item[2] for item in hits[: max(0, limit)]]

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        index_entries = self.store.load_index().get("entries") or []
        by_id = {
            entry.get("id"): entry
            for entry in index_entries
            if isinstance(entry, dict) and isinstance(entry.get("id"), str)
        }
        summaries: list[dict[str, Any]] = []
        for path in self._session_files():
            rows = self.store.read_rows(path.stem)
            entry = by_id.get(path.stem, {})
            summaries.append(
                {
                    "sessionId": path.stem,
                    "title": entry.get("summary") or "Session",
                    "createdAt": _timestamp(entry.get("createTime")),
                    "updatedAt": _timestamp(entry.get("updateTime"))
                    or (path.stat().st_mtime * 1000),
                    "turnCount": sum(1 for row in rows if _event_content(row)[0] == "user"),
                    "totalTokens": int(entry.get("activeTokens") or 0),
                }
            )
        summaries.sort(key=lambda item: float(item["updatedAt"]), reverse=True)
        return summaries[: max(0, limit)]

    def get_session_summary(self, session_id: str) -> dict[str, Any] | None:
        for summary in self.list_sessions(limit=10_000):
            if summary["sessionId"] == session_id:
                return summary
        return None
