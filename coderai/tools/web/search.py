"""WebSearch tool — multi-query web search over pluggable providers (Exa, Perplexity, DeepSeek, HTTP)."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from coderai.network.cache import build_search_key, get_search_cache
from coderai.tools.legacy.types import ToolResult, as_str
from coderai.web_providers import (
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
    cache = get_search_cache()

    # Single cache owner: provider-scoped key covers provider, normalized
    # query, and max_results, so a truncated or foreign-provider answer can
    # never be served as a hit. Error results are not cached.
    async def _fetch_unique(key: str, q: str) -> WebSearchResult:
        cached = cache.get(key)
        if cached is not None:
            return cached

        search_async_fn = getattr(provider, "search_async", None)
        if inspect.iscoroutinefunction(search_async_fn):
            res = await search_async_fn(q, max_results)
        else:
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(None, provider.search, q, max_results)

        if res is not None and res.error is None:
            cache.set(key, res)
        return res

    # Dedupe identical cache keys within one call: one fetch serves every
    # duplicate instead of racing duplicate misses through the provider.
    keys = [build_search_key(provider.id, q, max_results) for q in queries]
    query_by_key: dict[str, str] = {}
    for q, key in zip(queries, keys):
        query_by_key.setdefault(key, q)
    unique_keys = list(query_by_key)
    fetched = await asyncio.gather(*[_fetch_unique(key, query_by_key[key]) for key in unique_keys])
    result_by_key = dict(zip(unique_keys, fetched))
    results: list[WebSearchResult] = [result_by_key[key] for key in keys]

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
