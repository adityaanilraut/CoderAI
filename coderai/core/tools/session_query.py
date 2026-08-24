"""Model-facing session_query tool — search and query historical session context across workspace."""

from __future__ import annotations

import json
from typing import Any

from coderai.core.session_query.engine import SessionQueryEngine
from coderai.core.tools.types import ToolResult, as_str


async def handle_session_query_tool(args: dict[str, Any], context: Any) -> ToolResult:
    """Search and inspect historical sessions, messages, and tool logs across workspace."""
    action = as_str(args.get("action") or args.get("subcommand", "search")).strip().lower()
    query = as_str(args.get("query")).strip()
    session_id = as_str(args.get("session_id", "")).strip() or None
    role = as_str(args.get("role", "")).strip() or None
    limit = int(args.get("limit", 15))

    project_root = getattr(context, "project_root", ".") if context else "."
    engine = SessionQueryEngine(project_root=project_root)

    if action in ("search", "search_events", "search_messages"):
        if not query:
            return ToolResult(
                ok=False,
                name="session_query",
                error="Missing required argument 'query'.",
            )
        hits = engine.search_events(query=query, session_id=session_id, role=role, limit=limit)

        if not hits:
            return ToolResult(
                ok=True,
                name="session_query",
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
            name="session_query",
            output="\n".join(lines),
            metadata={"hits": hits, "results": hits, "query": query},
        )

    elif action in ("list", "list_sessions"):
        sessions = engine.list_sessions(limit=limit)
        if not sessions:
            return ToolResult(
                ok=True,
                name="session_query",
                output="No historical sessions recorded in workspace.",
                metadata={"sessions": []},
            )

        lines = [f"Recorded Sessions ({len(sessions)}):"]
        for s in sessions:
            lines.append(
                f"- `{s['sessionId']}`: {s['title']} (Turns: {s['turnCount']}, Tokens: {s['totalTokens']})"
            )

        return ToolResult(
            ok=True,
            name="session_query",
            output="\n".join(lines),
            metadata={"sessions": sessions},
        )

    elif action in ("summary", "get_summary"):
        target_id = session_id or (getattr(context, "session_id", None) if context else None)
        if not target_id:
            return ToolResult(
                ok=False,
                name="session_query",
                error="Missing required 'session_id' parameter.",
            )
        summary = engine.get_session_summary(target_id)
        if not summary:
            return ToolResult(
                ok=False,
                name="session_query",
                error=f"Session '{target_id}' not found in index.",
            )
        return ToolResult(
            ok=True,
            name="session_query",
            output=json.dumps(summary, indent=2),
            metadata=summary,
        )

    else:
        return ToolResult(
            ok=False,
            name="session_query",
            error=f"Unsupported action '{action}'. Supported: 'search', 'list', 'summary'.",
        )


# Alias
handle = handle_session_query_tool
