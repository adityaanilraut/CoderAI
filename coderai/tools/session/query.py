"""Read-only queries over persisted CoderAI sessions."""

from __future__ import annotations

import os
from typing import Any

from coderai.soul.session.models import SessionEntry, entry_from_dict
from coderai.soul.session.store import JsonlSessionStore
from coderai.tools.legacy.types import ToolResult

_MAX_HITS = 20
_SNIPPET = 180
_TRACE_LIMIT = 40
_READ_LIMIT = 30


def handle_session_search(args: dict[str, Any], context: Any) -> ToolResult:
    """Search session titles and summaries by keyword."""
    query = _required_text(args.get("query"))
    if query is None:
        return ToolResult(ok=False, name="session_search", error='Missing required "query".')
    _store, entries = _open(context)
    needle = query.lower()
    hits = [entry for entry in entries if needle in _entry_blob(entry)]
    if not hits:
        return ToolResult(ok=True, name="session_search", output=f"No sessions match {query!r}.")
    lines = [f"{len(hits)} session(s) match {query!r}:"]
    for entry in hits[:_MAX_HITS]:
        lines.append(
            f"- {entry.id} [{entry.status}] {entry.update_time or entry.create_time} "
            f"{_clip(entry.summary or '(no title)')}"
        )
    if len(hits) > _MAX_HITS:
        lines.append(f"... {len(hits) - _MAX_HITS} more")
    return ToolResult(ok=True, name="session_search", output="\n".join(lines))


def handle_session_trace(args: dict[str, Any], context: Any) -> ToolResult:
    """List recent events for one session."""
    session_id = _required_text(args.get("session_id"))
    if session_id is None:
        return ToolResult(ok=False, name="session_trace", error='Missing required "session_id".')
    store, entries = _open(context)
    resolved = _resolve_id(entries, session_id)
    if resolved is None:
        return ToolResult(ok=False, name="session_trace", error=f"Unknown session {session_id!r}.")
    events = store.list_events(resolved)
    if not events:
        return ToolResult(
            ok=True, name="session_trace", output=f"Session {resolved} has no events."
        )
    window = events[-_TRACE_LIMIT:]
    lines = [f"Session {resolved}: showing {len(window)} of {len(events)} events."]
    for event in window:
        lines.append(f"- #{event.seq} {event.type} {_clip(_event_text(event))}")
    return ToolResult(ok=True, name="session_trace", output="\n".join(lines))


def handle_session_event_search(args: dict[str, Any], context: Any) -> ToolResult:
    """Search event text inside one session."""
    session_id = _required_text(args.get("session_id"))
    query = _required_text(args.get("query"))
    if session_id is None or query is None:
        return ToolResult(
            ok=False,
            name="session_event_search",
            error='"session_id" and "query" are required.',
        )
    store, entries = _open(context)
    resolved = _resolve_id(entries, session_id)
    if resolved is None:
        return ToolResult(
            ok=False,
            name="session_event_search",
            error=f"Unknown session {session_id!r}.",
        )
    needle = query.lower()
    hits = [
        event
        for event in store.list_events(resolved)
        if needle in _event_text(event).lower() or needle in event.type.lower()
    ]
    if not hits:
        return ToolResult(
            ok=True,
            name="session_event_search",
            output=f"No events in {resolved} match {query!r}.",
        )
    lines = [f"{len(hits)} event(s) in {resolved} match {query!r}:"]
    for event in hits[:_MAX_HITS]:
        lines.append(f"- #{event.seq} {event.type} {_clip(_event_text(event))}")
    if len(hits) > _MAX_HITS:
        lines.append(f"... {len(hits) - _MAX_HITS} more")
    return ToolResult(ok=True, name="session_event_search", output="\n".join(lines))


def handle_session_event_read(args: dict[str, Any], context: Any) -> ToolResult:
    """Read a slice of one session's event log."""
    session_id = _required_text(args.get("session_id"))
    if session_id is None:
        return ToolResult(
            ok=False, name="session_event_read", error='Missing required "session_id".'
        )
    store, entries = _open(context)
    resolved = _resolve_id(entries, session_id)
    if resolved is None:
        return ToolResult(
            ok=False, name="session_event_read", error=f"Unknown session {session_id!r}."
        )
    events = store.list_events(resolved)
    offset = _nonneg_int(args.get("offset"), default=0)
    limit = _nonneg_int(args.get("limit"), default=_READ_LIMIT)
    limit = min(limit or _READ_LIMIT, _READ_LIMIT)
    window = events[offset : offset + limit]
    if not window:
        return ToolResult(
            ok=True,
            name="session_event_read",
            output=f"No events at offset {offset} in {resolved} ({len(events)} total).",
        )
    lines = [f"Session {resolved} events {offset}..{offset + len(window) - 1} of {len(events)}:"]
    for event in window:
        lines.append(f"- #{event.seq} {event.type}\n  {_clip(_event_text(event), 500)}")
    return ToolResult(ok=True, name="session_event_read", output="\n".join(lines))


def _open(context: Any) -> tuple[JsonlSessionStore, list[SessionEntry]]:
    manager = getattr(context, "session_manager", None)
    store = getattr(manager, "session_store", None) if manager is not None else None
    if isinstance(store, JsonlSessionStore):
        if hasattr(manager, "list_sessions"):
            listed = manager.list_sessions()
            if isinstance(listed, list):
                return store, [item for item in listed if isinstance(item, SessionEntry)]
        return store, _entries_from_store(store)
    root = getattr(context, "project_root", None) or os.getcwd()
    opened = JsonlSessionStore(str(root), cleanup=False)
    return opened, _entries_from_store(opened)


def _entries_from_store(store: JsonlSessionStore) -> list[SessionEntry]:
    entries = store.load_index().get("entries") or []
    return [entry_from_dict(item) for item in entries if isinstance(item, dict) and item.get("id")]


def _resolve_id(entries: list[SessionEntry], raw: str) -> str | None:
    exact = [entry.id for entry in entries if entry.id == raw]
    if exact:
        return exact[0]
    prefix = [entry.id for entry in entries if entry.id.startswith(raw)]
    if len(prefix) == 1:
        return prefix[0]
    return None


def _entry_blob(entry: SessionEntry) -> str:
    parts = [entry.id, entry.summary or "", entry.status or "", entry.assistant_reply or ""]
    return " ".join(parts).lower()


def _event_text(event: Any) -> str:
    data = getattr(event, "data", None)
    if isinstance(data, dict):
        content = data.get("content") or data.get("text") or ""
        if isinstance(content, str):
            return content
        return str(content)
    return ""


def _required_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _nonneg_int(value: Any, *, default: int) -> int:
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return default


def _clip(text: str, limit: int = _SNIPPET) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "..."
