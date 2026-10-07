"""Built-in tool definitions owned by this subsystem."""

from __future__ import annotations
from typing import Any
from coderai.tools.legacy.policy import builtin_effect_policy
from coderai.tools.web import fetch as _fetch
from coderai.tools.web import search as _search
from coderai.tools.legacy.schema import define_tool


def register_tools(registry: Any) -> None:
    registry.register(
        define_tool(
            effects=builtin_effect_policy("WebSearch", False),
            name="WebSearch",
            description="Search the web for up-to-date documentation, issues, and references.",
            parameters={
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2000,
                    "description": "One search query. Provide query or queries.",
                },
                "queries": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "minItems": 1,
                    "maxItems": 4,
                    "description": "Up to four queries searched concurrently.",
                },
                "provider": {
                    "type": "string",
                    "description": "Optional provider id: http, exa, perplexity, deepseek, custom, or a registered provider.",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Maximum returned sources.",
                },
                "use_cache": {
                    "type": "boolean",
                    "description": "Set false to request fresh results.",
                },
                "include_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 20,
                    "description": "Only return these domains; *.example.com includes subdomains.",
                },
                "exclude_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 20,
                    "description": "Exclude these domains.",
                },
                "start_date": {
                    "type": "string",
                    "description": "Publication start date (ISO 8601); requires Exa or Perplexity.",
                },
                "end_date": {
                    "type": "string",
                    "description": "Publication end date (ISO 8601); requires Exa or Perplexity.",
                },
                "recency_days": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 3650,
                    "description": "Publication age in days; requires Exa or Perplexity.",
                },
                "language": {
                    "type": "string",
                    "description": "ISO language code; requires Perplexity.",
                },
                "region": {
                    "type": "string",
                    "description": "Two-letter country code for regional results.",
                },
            },
            required=[],
            handler=_search.handle_web_search_tool,
            category="web",
            rate_limited_id="WebSearch",
            is_mutating=False,
            is_concurrency_safe=True,
            timeout_ms=60_000,
        )
    )

    registry.register(
        define_tool(
            effects=builtin_effect_policy("WebFetch", False),
            name="WebFetch",
            description="Fetch and extract readable Markdown content from a public URL.",
            parameters={
                "url": {
                    "type": "string",
                    "description": "The URL to fetch and convert to Markdown.",
                },
                "raw": {
                    "type": "boolean",
                    "description": "Whether to return the raw HTML instead of Markdown.",
                },
                "max_length": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100_000,
                    "description": "Maximum number of characters to return.",
                },
                "use_cache": {
                    "type": "boolean",
                    "description": "Whether to use the HTTP response cache.",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 5_000_000,
                    "description": "Character offset to continue a truncated response; use nextOffset from the previous result.",
                },
                "section": {
                    "type": "string",
                    "description": "Read a specific Markdown heading or PDF page heading, e.g. Page 2.",
                },
            },
            required=["url"],
            handler=_fetch.handle_web_fetch_tool,
            category="web",
            rate_limited_id="WebFetch",
            is_mutating=False,
            is_concurrency_safe=True,
            timeout_ms=60_000,
        )
    )
