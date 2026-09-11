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
    data = row.get("data")
    if isinstance(data, dict):
        event_type = str(row.get("type", ""))
        role = {
            USER_MESSAGE: "user",
            STEERING_MESSAGE: "user",
            ASSISTANT_MESSAGE: "assistant",
            TOOL_CALL: "assistant",
            TOOL_RESULT: "tool",
            COMPACTION_SUMMARY: "system",
        }.get(event_type, data.get("role", ""))
        content = data.get("content")
        if not isinstance(content, str):
            content = json.dumps(data, ensure_ascii=False) if data else ""
        tool_name = data.get("name") or data.get("toolName")
        return str(role), content, str(tool_name) if tool_name else None

    # Direct / legacy row format
    role = str(row.get("role") or "")
    content = row.get("content")
    if not isinstance(content, str):
        content = json.dumps(row, ensure_ascii=False) if row else ""
    tool_name = row.get("name") or row.get("tool_name")
    return (
        role,
        content,
        str(tool_name) if tool_name else None,
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

    def search_sessions(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Search prior sessions in caller workspace by title, summary, or content match."""
        terms = re.findall(r"\w+", query.lower())
        if not terms:
            return self.list_sessions(limit=limit)

        all_summaries = self.list_sessions(limit=1000)
        scored: list[tuple[int, dict[str, Any]]] = []
        for s in all_summaries:
            text = f"{s['title']} {s['sessionId']}".lower()
            score = sum(text.count(t) * 10 for t in terms)
            # Also search events for deeper hits
            event_hits = self.search_events(query, session_id=s["sessionId"], limit=5)
            score += len(event_hits)
            if score > 0:
                s_copy = dict(s)
                s_copy["matchCount"] = len(event_hits)
                scored.append((score, s_copy))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [s for _, s in scored[: max(0, limit)]]

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

    def get_session_trace(self, session_id: str) -> dict[str, Any]:
        """Read the authorized session lineage and metadata."""
        summary = self.get_session_summary(session_id) or {"sessionId": session_id}
        rows = self.store.read_rows(session_id)
        events_summary = []
        for fallback_seq, row in enumerate(rows):
            role, content, tool_name = _event_content(row)
            events_summary.append(
                {
                    "seq": row.get("seq", fallback_seq),
                    "role": role,
                    "toolName": tool_name,
                    "chars": len(content),
                    "type": row.get("type", "message"),
                }
            )
        return {
            "session": summary,
            "totalEvents": len(rows),
            "events": events_summary,
        }

    def get_event(
        self,
        session_id: str,
        seq: int,
        window: int = 2,
    ) -> dict[str, Any] | None:
        """Read one full unabridged event and neighboring event summaries."""
        rows = self.store.read_rows(session_id)
        target_row = None
        target_idx = -1
        for idx, row in enumerate(rows):
            if row.get("seq", idx) == seq:
                target_row = row
                target_idx = idx
                break

        if not target_row:
            return None

        role, content, tool_name = _event_content(target_row)
        neighbors = []
        start = max(0, target_idx - window)
        end = min(len(rows), target_idx + window + 1)
        for i in range(start, end):
            if i != target_idx:
                r, c, t = _event_content(rows[i])
                neighbors.append(
                    {
                        "seq": rows[i].get("seq", i),
                        "role": r,
                        "toolName": t,
                        "preview": c[:100],
                    }
                )

        return {
            "sessionId": session_id,
            "seq": seq,
            "role": role,
            "toolName": tool_name,
            "content": content,
            "raw": target_row,
            "neighbors": neighbors,
        }
