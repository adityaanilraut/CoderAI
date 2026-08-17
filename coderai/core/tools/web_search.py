"""WebSearch tool — search the web via a configured search backend (deepcode web-search-handler.ts)."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
import uuid
from typing import Any

from coderai.core.tools.types import ToolResult, as_str

WEB_SEARCH_ACTIVITY_PREFIX = "WebSearch:"


def _format_activity_label(query: str) -> str:
    normalized = " ".join(query.split()).strip()
    max_len = 180
    clipped = f"{normalized[: max_len - 3]}..." if len(normalized) > max_len else normalized
    return f"{WEB_SEARCH_ACTIVITY_PREFIX} {clipped}"


async def handle(args: dict[str, Any], context: Any) -> ToolResult:
    return await handle_web_search_tool(args, context)


async def handle_web_search_tool(args: dict[str, Any], context: Any) -> ToolResult:
    query = as_str(args.get("query")).strip()
    if not query:
        return ToolResult(
            ok=False,
            name="WebSearch",
            error='Missing required "query" string.',
        )

    activity_id = f"web-search-{uuid.uuid4()}"
    on_process_start = getattr(context, "on_process_start", None) or (
        context.get("on_process_start") if isinstance(context, dict) else None
    )
    on_process_exit = getattr(context, "on_process_exit", None) or (
        context.get("on_process_exit") if isinstance(context, dict) else None
    )
    on_rate_limit = getattr(context, "on_plugin_rate_limit_exceeded", None) or (
        context.get("on_plugin_rate_limit_exceeded") if isinstance(context, dict) else None
    )

    if on_process_start:
        on_process_start(activity_id, _format_activity_label(query))

    try:
        results = []
        encoded = urllib.parse.urlencode(
            {
                "q": query,
                "format": "json",
                "no_html": "1",
                "skip_disambig": "1",
            }
        )
        url = f"https://api.duckduckgo.com/?{encoded}"
        req = urllib.request.Request(url, headers={"User-Agent": "CoderAI/1.0"})

        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))
                for item in data.get("RelatedTopics", [])[:5]:
                    if isinstance(item, dict) and item.get("Text"):
                        results.append(f"- {item['Text']}")
                abstract = data.get("AbstractText")
                if abstract:
                    results.insert(0, abstract)
        except urllib.error.HTTPError as http_err:
            if http_err.code == 429 and on_rate_limit:
                on_rate_limit("WebSearch")
        except Exception:
            # If network request fails or is blocked, return clean message
            pass

        if not results:
            return ToolResult(
                ok=True,
                name="WebSearch",
                output=f"No online results found for '{query}'.",
                metadata={"query": query},
            )

        return ToolResult(
            ok=True,
            name="WebSearch",
            output="\n".join(results),
            metadata={"query": query},
        )
    finally:
        if on_process_exit:
            on_process_exit(activity_id)
