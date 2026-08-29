"""Model-facing session query tools — search and query historical session context across workspace."""

from __future__ import annotations

import json
from typing import Any

from coderai.core.session_query.engine import SessionQueryEngine
from coderai.core.tools.types import ToolResult, as_str


async def handle_session_search_tool(args: dict[str, Any], context: Any) -> ToolResult:
    """Search prior sessions in the caller workspace and return the strongest matching sessions."""
    query = as_str(args.get("query")).strip()
    limit = int(args.get("limit", 10))
    project_root = getattr(context, "project_root", ".") if context else "."
    engine = SessionQueryEngine(project_root=project_root)

    if not query:
        sessions = engine.list_sessions(limit=limit)
    else:
        sessions = engine.search_sessions(query=query, limit=limit)

    if not sessions:
        return ToolResult(
            ok=True,
            name="session_search",
            output=f"No sessions found{' matching query: ' + repr(query) if query else ' in workspace'}.",
            metadata={"sessions": []},
        )

    lines = [f"Found {len(sessions)} matching session(s):"]
    for s in sessions:
        lines.append(
            f"- Session `{s['sessionId']}`: {s['title']} (Turns: {s['turnCount']}, Tokens: {s['totalTokens']})"
        )

    return ToolResult(
        ok=True,
        name="session_search",
        output="\n".join(lines),
        metadata={"sessions": sessions},
    )


async def handle_session_trace_tool(args: dict[str, Any], context: Any) -> ToolResult:
    """Read the authorized session lineage and event summaries around one session."""
    session_id = as_str(args.get("session_id", "")).strip() or (
        getattr(context, "session_id", "") if context else ""
    )
    if not session_id:
        return ToolResult(
            ok=False,
            name="session_trace",
            error="Missing required argument 'session_id'.",
        )

    project_root = getattr(context, "project_root", ".") if context else "."
    engine = SessionQueryEngine(project_root=project_root)
    trace = engine.get_session_trace(session_id)

    lines = [
        f"### Session Trace: `{session_id}`",
        f"- **Title**: {trace['session'].get('title', 'Untitled')}",
        f"- **Total Events**: {trace['totalEvents']}",
        "\n**Recent Events**:",
    ]
    for ev in trace["events"][-15:]:
        tool_tag = f" [{ev['toolName']}]" if ev.get("toolName") else ""
        lines.append(f"- seq {ev['seq']} | `{ev['role']}`{tool_tag} ({ev['chars']} chars)")

    return ToolResult(
        ok=True,
        name="session_trace",
        output="\n".join(lines),
        metadata=trace,
    )


async def handle_session_event_search_tool(args: dict[str, Any], context: Any) -> ToolResult:
    """Search prior events in authorized sessions."""
    query = as_str(args.get("query")).strip()
    session_id = as_str(args.get("session_id", "")).strip() or None
    role = as_str(args.get("role", "")).strip() or None
    limit = int(args.get("limit", 15))

    if not query:
        return ToolResult(
            ok=False,
            name="session_event_search",
            error="Missing required argument 'query'.",
        )

    project_root = getattr(context, "project_root", ".") if context else "."
    engine = SessionQueryEngine(project_root=project_root)
    hits = engine.search_events(query=query, session_id=session_id, role=role, limit=limit)

    if not hits:
        return ToolResult(
            ok=True,
            name="session_event_search",
            output=f"No matching session messages found for query: '{query}'.",
            metadata={"hits": [], "results": [], "query": query},
        )

    lines = [f"Found {len(hits)} matching session event(s):"]
    for i, hit in enumerate(hits, 1):
        tool_info = f" [{hit['toolName']}]" if hit.get("toolName") else ""
        lines.append(
            f"{i}. Session `{hit['sessionId']}` (seq {hit['seq']}, {hit['role']}{tool_info}):"
        )
        lines.append(f"   {hit['snippet'].strip()}")

    return ToolResult(
        ok=True,
        name="session_event_search",
        output="\n".join(lines),
        metadata={"hits": hits, "results": hits, "query": query},
    )


async def handle_session_event_read_tool(args: dict[str, Any], context: Any) -> ToolResult:
    """Read one full unabridged event and optional neighboring event summaries."""
    session_id = as_str(args.get("session_id", "")).strip() or (
        getattr(context, "session_id", "") if context else ""
    )
    if not session_id:
        return ToolResult(
            ok=False,
            name="session_event_read",
            error="Missing required argument 'session_id'.",
        )

    try:
        seq = int(args.get("seq", 0))
    except (ValueError, TypeError):
        seq = 0

    project_root = getattr(context, "project_root", ".") if context else "."
    engine = SessionQueryEngine(project_root=project_root)
    event_data = engine.get_event(session_id=session_id, seq=seq)

    if not event_data:
        return ToolResult(
            ok=False,
            name="session_event_read",
            error=f"Event at sequence {seq} in session '{session_id}' not found.",
        )

    lines = [
        f"### Event Detail: Session `{session_id}` (seq {seq})",
        f"- **Role**: `{event_data['role']}`",
    ]
    if event_data.get("toolName"):
        lines.append(f"- **Tool**: `{event_data['toolName']}`")
    lines.append(f"\n**Content**:\n{event_data['content']}")

    return ToolResult(
        ok=True,
        name="session_event_read",
        output="\n".join(lines),
        metadata=event_data,
    )


async def handle_session_query_tool(args: dict[str, Any], context: Any) -> ToolResult:
    """Unified search and query tool across historical sessions and events."""
    action = as_str(args.get("action") or args.get("subcommand", "search")).strip().lower()

    if action in ("trace", "session_trace"):
        return await handle_session_trace_tool(args, context)
    elif action in ("read_event", "event_read", "get_event"):
        return await handle_session_event_read_tool(args, context)
    elif action in ("list", "list_sessions", "search_sessions"):
        res = await handle_session_search_tool(args, context)
        # Preserve session_query tool name if invoked as session_query
        return ToolResult(
            ok=res.ok,
            name="session_query",
            output=res.output,
            error=res.error,
            metadata=res.metadata,
        )
    elif action in ("summary", "get_summary"):
        session_id = as_str(args.get("session_id", "")).strip() or (
            getattr(context, "session_id", None) if context else None
        )
        if not session_id:
            return ToolResult(
                ok=False,
                name="session_query",
                error="Missing required 'session_id' parameter.",
            )
        project_root = getattr(context, "project_root", ".") if context else "."
        engine = SessionQueryEngine(project_root=project_root)
        summary = engine.get_session_summary(session_id)
        if not summary:
            return ToolResult(
                ok=False,
                name="session_query",
                error=f"Session '{session_id}' not found in index.",
            )
        return ToolResult(
            ok=True,
            name="session_query",
            output=json.dumps(summary, indent=2),
            metadata=summary,
        )
    else:
        res = await handle_session_event_search_tool(args, context)
        return ToolResult(
            ok=res.ok,
            name="session_query",
            output=res.output,
            error=res.error,
            metadata=res.metadata,
        )
