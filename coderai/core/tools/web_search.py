"""WebSearch tool — multi-query web search over pluggable providers (Exa, Perplexity, DeepSeek, HTTP)."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from coderai.core.network.cache import get_search_cache
from coderai.core.tools.types import ToolResult, as_str
from coderai.core.web_providers import (
    WebSearchResult,
    resolve_web_search_provider,
)

MAX_QUERIES = 4
DEFAULT_MAX_RESULTS = 8


async def handle_web_search_tool(args: dict[str, Any], context: Any) -> ToolResult:
    """Execute one or more web search queries via the configured WebSearchProvider."""
    raw_queries = args.get("queries") or args.get("query")
    if not raw_queries:
        return ToolResult(
            ok=False,
            name="WebSearch",
            error="Missing required argument 'query' or 'queries'.",
        )

    queries: list[str] = []
    if isinstance(raw_queries, str):
        q = raw_queries.strip()
        if q:
            queries.append(q)
    elif isinstance(raw_queries, list):
        for item in raw_queries:
            if isinstance(item, str) and item.strip():
                queries.append(item.strip())

    if not queries:
        return ToolResult(
            ok=False,
            name="WebSearch",
            error="No valid non-empty search query provided.",
        )

    # Bound query count
    if len(queries) > MAX_QUERIES:
        queries = queries[:MAX_QUERIES]

    max_results = int(args.get("max_results", DEFAULT_MAX_RESULTS))
    provider_name = as_str(args.get("provider", "")).strip() or None

    provider = resolve_web_search_provider(provider_name)

    # Execute search for each query
    async def _search_one(q: str) -> WebSearchResult:
        cache = get_search_cache()
        cached = cache.get(f"search:{q}")
        if cached:
            return cached

        search_async_fn = getattr(provider, "search_async", None)
        if inspect.iscoroutinefunction(search_async_fn):
            return await search_async_fn(q, max_results)

        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, provider.search, q, max_results)
        return res

    results: list[WebSearchResult] = await asyncio.gather(*[_search_one(q) for q in queries])

    # Format output for LLM
    output_lines: list[str] = []
    metadata_results: list[dict[str, Any]] = []
    all_sources: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for res in results:
        metadata_results.append(res.to_dict())
        for s in res.sources:
            if s.url and s.url not in seen_urls:
                seen_urls.add(s.url)
                all_sources.append(s.to_dict())

        output_lines.append(f"## Search Results: `{res.query}`")

        if res.error:
            output_lines.append(f"> ⚠️ **Search Error**: {res.error}\n")
            continue

        if res.content:
            output_lines.append(f"### Direct Summary\n{res.content}\n")

        if res.sources:
            output_lines.append("### Sources:")
            for i, src in enumerate(res.sources, 1):
                date_str = f" ({src.published_at})" if src.published_at else ""
                output_lines.append(f"{i}. **[{src.title}]({src.url})**{date_str}")
                if src.snippet:
                    output_lines.append(f"   > {src.snippet}")
            output_lines.append("")
        elif not res.content:
            output_lines.append("*(No results found for query)*\n")

    return ToolResult(
        ok=True,
        name="WebSearch",
        output="\n".join(output_lines).strip(),
        metadata={
            "provider": provider.id,
            "query": ", ".join(queries),
            "results": metadata_results,
            "sources": all_sources,
        },
    )


# Alias for backward compatibility
handle = handle_web_search_tool
