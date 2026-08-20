"""Tool handler for session_query."""

from __future__ import annotations

from typing import Any

from coderai.core.session_query.indexer import get_session_index
from coderai.core.tools.types import ToolExecutionContext, ToolResult, as_str


async def handle_session_query_tool(
    args: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    """Perform full-text search over past session messages and tool results."""
    query = as_str(args.get("query", "")).strip()
    if not query:
        return ToolResult(
            ok=False,
            name="session_query",
            error="Missing required argument 'query'.",
        )

    session_id = as_str(args.get("session_id", "")).strip() or None
    role = as_str(args.get("role", "")).strip() or None
    try:
        limit = int(args.get("limit", 10))
        if limit <= 0:
            limit = 10
    except (ValueError, TypeError):
        limit = 10

    index = get_session_index(context.project_root)
    # Refresh index from workspace
    index.scan_and_index_workspace()

    results = index.search(
        query=query,
        session_id=session_id,
        role=role,
        limit=limit,
    )

    if not results:
        return ToolResult(
            ok=True,
            name="session_query",
            output=f"No matching session messages found for query: '{query}'.",
            metadata={"results": [], "query": query},
        )

    lines = [f"### Session Query Results for: `{query}` ({len(results)} matches)\n"]
    for idx, r in enumerate(results, 1):
        tool_info = f" [Tool: `{r.tool_name}`]" if r.tool_name else ""
        lines.append(
            f"**{idx}. [{r.role.upper()}]{tool_info}** (Score: `{r.score}`, Session: `{r.session_id}`, Msg: `{r.message_id}`)\n"
            f"> {r.content_snippet}\n"
        )

    return ToolResult(
        ok=True,
        name="session_query",
        output="\n".join(lines),
        metadata={"results": [r.to_dict() for r in results], "query": query},
    )
