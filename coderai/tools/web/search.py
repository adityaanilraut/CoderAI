"""Bounded multi-query web search with provider fallback and freshness metadata."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from typing import Any

from coderai.network.cache import build_search_key, get_search_cache
from coderai.network.security import NetworkPolicy, NetworkSecurityError
from coderai.tools.legacy.types import ToolResult, as_str
from coderai.tools.web.common import (
    DEFAULT_OUTPUT_CHARS,
    EXTERNAL_CONTENT_NOTICE,
    bounded_int,
    safe_web_url,
    sanitize_prompt_injection,
    slice_payload,
    web_settings,
)
from coderai.tools.web.options import SearchFilters
from coderai.utils.aiohttp import HttpClient
from coderai.web_providers import (
    WebSearchProvider,
    WebSearchResult,
    WebSearchSource,
    resolve_web_search_provider,
)

MAX_QUERIES = 4
DEFAULT_MAX_RESULTS = 8


def _normalize_result(
    result: WebSearchResult, query: str, limit: int, filters: SearchFilters, budget: int = 20_000
) -> WebSearchResult:
    """Bound both model text and metadata; search sources stay external data."""
    sources: list[WebSearchSource] = []
    seen: set[str] = set()
    remaining = max(0, budget - min(len(as_str(result.content)), 4000) - len(query) - 500)
    for source in result.sources:
        url = safe_web_url(as_str(source.url))
        if not url or len(url) > 2048 or url in seen or not filters.allows_url(url):
            continue
        normalized = WebSearchSource(
            title=sanitize_prompt_injection(as_str(source.title))[:200],
            url=url,
            snippet=sanitize_prompt_injection(as_str(source.snippet))[:500] or None,
            published_at=sanitize_prompt_injection(as_str(source.published_at))[:64] or None,
        )
        size = len(normalized.title) + len(url) + len(normalized.snippet or "") + 64
        if size > remaining:
            break
        seen.add(url)
        remaining -= size
        sources.append(normalized)
        if len(sources) >= limit:
            break
    # Drop summaries with strict domain constraints: synthesized answers may
    # cite excluded sources even when the returned source list is filtered.
    content = (
        None
        if filters.include_domains or filters.exclude_domains
        else (sanitize_prompt_injection(as_str(result.content))[:4000] or None)
    )
    return WebSearchResult(
        query=query,
        sources=sources,
        content=content,
        error=sanitize_prompt_injection(as_str(result.error))[:1000] or None,
        fetched_at=result.fetched_at,
        from_cache=result.from_cache,
        truncated=result.truncated
        or len(sources) < min(limit, len(result.sources))
        or len(as_str(result.content)) > 4000,
    )


async def handle_web_search_tool(args: dict[str, Any], context: Any) -> ToolResult:
    raw_queries = args.get("queries") if "queries" in args else args.get("query")
    if "queries" in args and "query" in args:
        return ToolResult(ok=False, name="WebSearch", error="Provide query or queries, not both.")
    if not raw_queries:
        return ToolResult(
            ok=False, name="WebSearch", error="Missing required argument 'query' or 'queries'."
        )
    queries = [raw_queries] if isinstance(raw_queries, str) else raw_queries
    if not isinstance(queries, list) or not 1 <= len(queries) <= MAX_QUERIES:
        return ToolResult(
            ok=False, name="WebSearch", error=f"Provide between 1 and {MAX_QUERIES} search queries."
        )
    if any(not isinstance(query, str) or not query.strip() for query in queries):
        return ToolResult(
            ok=False, name="WebSearch", error="No valid non-empty search query provided."
        )
    queries = [query.strip() for query in queries]
    if any(len(query) > 2000 for query in queries):
        return ToolResult(
            ok=False, name="WebSearch", error="Each query must be at most 2000 characters."
        )
    try:
        max_results = bounded_int(
            args.get("max_results"), DEFAULT_MAX_RESULTS, 1, 50, "max_results"
        )
        filters = SearchFilters.from_args(args)
        settings = web_settings(context)
        policy = NetworkPolicy.from_settings(settings)
    except ValueError as exc:
        return ToolResult(ok=False, name="WebSearch", error=str(exc))
    provider_name = as_str(args.get("provider")).strip() or None
    use_cache = bool(args.get("use_cache", True))
    cache = get_search_cache()
    owned_client = HttpClient(policy=policy) if settings.get("network") else None
    try:
        try:
            provider = resolve_web_search_provider(
                provider_name, settings=settings, filters=filters, client=owned_client
            )
        except ValueError as exc:
            return ToolResult(ok=False, name="WebSearch", error=str(exc))
        scope = (
            provider.cache_scope()
            if isinstance(provider, WebSearchProvider)
            else {"id": provider.id}
        )
        scope["output_budget"] = DEFAULT_OUTPUT_CHARS // len(queries)
        key_suffix = json.dumps(scope, sort_keys=True, default=str)
        # Metadata stays bounded even for four queries with 50 requested results.
        per_query_limit = max_results

        async def fetch(query: str) -> tuple[WebSearchResult, str, str | None]:
            key = build_search_key(provider.id, query, max_results) + ":" + key_suffix
            if use_cache:
                cached = cache.get(key)
                if isinstance(cached, WebSearchResult):
                    cached.from_cache = True
                    cached.query = query
                    return cached, provider.id, None

            async def invoke(selected: Any) -> WebSearchResult:
                async_search = getattr(selected, "search_async", None)
                if inspect.iscoroutinefunction(async_search):
                    result = await async_search(query, max_results)
                else:
                    result = await asyncio.to_thread(selected.search, query, max_results)
                if not isinstance(result, WebSearchResult):
                    raise ValueError("Provider returned an invalid search result.")
                return _normalize_result(
                    result, query, per_query_limit, filters, DEFAULT_OUTPUT_CHARS // len(queries)
                )

            selected = provider
            warning = None
            try:
                result = await invoke(selected)
                if (
                    result.error
                    and selected.id != "http"
                    and selected.id in {"exa", "perplexity", "deepseek"}
                ):
                    warning = f"{selected.id} failed: {result.error}"
                    selected = resolve_web_search_provider(
                        "http", settings=settings, filters=filters, client=owned_client
                    )
                    result = await invoke(selected)
            except NetworkSecurityError as exc:
                # A denial is terminal for this query; it never triggers fallback.
                return (
                    WebSearchResult(query=query, error=f"Security Policy Violation: {exc}"),
                    selected.id,
                    None,
                )
            except Exception as exc:
                return (
                    WebSearchResult(query=query, error=sanitize_prompt_injection(str(exc))[:1000]),
                    selected.id,
                    warning,
                )
            result.fetched_at = time.time()
            if use_cache and not result.error and selected.id == provider.id:
                cache.set(key, result)
            return result, selected.id, warning

        unique_queries = list(dict.fromkeys(queries))
        fetched = await asyncio.gather(*(fetch(query) for query in unique_queries))
        lines = [EXTERNAL_CONTENT_NOTICE, ""]
        results_metadata: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        successes = 0
        for result, selected_id, warning in fetched:
            item = result.to_dict()
            item["provider"] = selected_id
            item["cacheAgeSeconds"] = (
                max(0, time.time() - result.fetched_at) if result.fetched_at else 0
            )
            if warning:
                item["warning"] = warning
            results_metadata.append(item)
            lines.append(f"## Search Results: `{result.query}`")
            if result.error:
                lines.append(f"Search Error: {result.error}")
                continue
            successes += 1
            if warning:
                lines.append(f"Provider fallback: {warning}")
            if result.content:
                lines.append(f"### Direct Summary\n{result.content}")
            for source in result.sources:
                source_dict = source.to_dict()
                if source.url not in seen_urls and len(sources) < max_results:
                    seen_urls.add(source.url)
                    sources.append(source_dict)
                lines.append(
                    f"- [{source.title}]({source.url})"
                    + (f" ({source.published_at})" if source.published_at else "")
                )
                if source.snippet:
                    lines.append(f"  {source.snippet}")
            if not result.sources and not result.content:
                lines.append("No results found for query.")
            lines.append("")
        output, truncated = slice_payload(
            "\n".join(lines).strip(), DEFAULT_OUTPUT_CHARS, continuation=False
        )
        errors = [item["error"] for item in results_metadata if item.get("error")]
        return ToolResult(
            ok=successes > 0,
            name="WebSearch",
            output=output,
            error="; ".join(errors) if not successes else None,
            metadata={
                "provider": provider.id,
                "query": ", ".join(queries),
                "results": results_metadata,
                "sources": sources,
                "filters": filters.to_dict(),
                "partialFailure": bool(successes and errors),
                "untrusted": True,
                "truncated": truncated or any(item.get("truncated") for item in results_metadata),
                "useCache": use_cache,
            },
        )
    finally:
        if owned_client:
            owned_client.close()


handle = handle_web_search_tool
