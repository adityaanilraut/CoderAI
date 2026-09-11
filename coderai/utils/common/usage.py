"""Token usage extraction and accumulation utilities for DeepSeek, OpenAI, and Anthropic prompt caching."""

from __future__ import annotations

from typing import Any


def extract_usage_dict(raw: Any) -> dict[str, int]:
    """Extract standard token counts and prompt caching metrics from any API usage response object or dict."""
    if not raw:
        return {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
            "uncached_tokens": 0,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }

    if isinstance(raw, dict):
        p = int(raw.get("prompt_tokens") or raw.get("input_tokens") or 0)
        c = int(raw.get("completion_tokens") or raw.get("output_tokens") or 0)
        tot = int(raw.get("total_tokens") or (p + c))
        details = raw.get("prompt_tokens_details")
        cached_from_details = 0
        if isinstance(details, dict):
            cached_from_details = int(details.get("cached_tokens") or 0)
        elif hasattr(details, "cached_tokens"):
            cached_from_details = int(getattr(details, "cached_tokens", 0) or 0)

        cached = int(
            raw.get("prompt_cache_hit_tokens")
            or cached_from_details
            or raw.get("cached_tokens")
            or raw.get("cache_read_input_tokens")
            or 0
        )
        creation = int(
            raw.get("cache_creation_input_tokens") or raw.get("prompt_cache_creation_tokens") or 0
        )
        miss = raw.get("prompt_cache_miss_tokens")
        if miss is None:
            miss = max(0, p - cached)
        else:
            miss = int(miss)

        uncached = miss if miss is not None else max(0, p - cached)

        return {
            "prompt_tokens": p,
            "completion_tokens": c,
            "total_tokens": tot,
            "cached_tokens": cached,
            "uncached_tokens": uncached,
            "prompt_cache_hit_tokens": cached,
            "prompt_cache_miss_tokens": miss,
            "cache_creation_input_tokens": creation,
            "cache_read_input_tokens": cached,
        }

    p = int(getattr(raw, "prompt_tokens", None) or getattr(raw, "input_tokens", 0) or 0)
    c = int(getattr(raw, "completion_tokens", None) or getattr(raw, "output_tokens", 0) or 0)
    tot = int(getattr(raw, "total_tokens", 0) or (p + c))

    details = getattr(raw, "prompt_tokens_details", None)
    cached_from_details = 0
    if isinstance(details, dict):
        cached_from_details = int(details.get("cached_tokens") or 0)
    elif hasattr(details, "cached_tokens"):
        cached_from_details = int(getattr(details, "cached_tokens", 0) or 0)

    cached = int(
        getattr(raw, "prompt_cache_hit_tokens", None)
        or cached_from_details
        or getattr(raw, "cached_tokens", 0)
        or getattr(raw, "cache_read_input_tokens", 0)
        or 0
    )
    creation = int(
        getattr(raw, "cache_creation_input_tokens", 0)
        or getattr(raw, "prompt_cache_creation_tokens", 0)
        or 0
    )
    miss_attr = getattr(raw, "prompt_cache_miss_tokens", None)
    if miss_attr is None:
        miss = max(0, p - cached)
    else:
        miss = int(miss_attr)

    uncached = miss if miss is not None else max(0, p - cached)

    return {
        "prompt_tokens": p,
        "completion_tokens": c,
        "total_tokens": tot,
        "cached_tokens": cached,
        "uncached_tokens": uncached,
        "prompt_cache_hit_tokens": cached,
        "prompt_cache_miss_tokens": miss,
        "cache_creation_input_tokens": creation,
        "cache_read_input_tokens": cached,
    }


def accumulate_usage_dict(
    current: dict[str, Any] | None, usage: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Accumulate usage metrics including prompt cache hits and misses."""
    if usage is None:
        return current
    c = current or {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        "uncached_tokens": 0,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    extracted = extract_usage_dict(usage)
    return {
        "prompt_tokens": c.get("prompt_tokens", 0) + extracted["prompt_tokens"],
        "completion_tokens": c.get("completion_tokens", 0) + extracted["completion_tokens"],
        "total_tokens": c.get("total_tokens", 0) + extracted["total_tokens"],
        "cached_tokens": c.get("cached_tokens", 0) + extracted["cached_tokens"],
        "uncached_tokens": c.get("uncached_tokens", 0) + extracted["uncached_tokens"],
        "prompt_cache_hit_tokens": c.get("prompt_cache_hit_tokens", 0)
        + extracted["prompt_cache_hit_tokens"],
        "prompt_cache_miss_tokens": c.get("prompt_cache_miss_tokens", 0)
        + extracted["prompt_cache_miss_tokens"],
        "cache_creation_input_tokens": c.get("cache_creation_input_tokens", 0)
        + extracted["cache_creation_input_tokens"],
        "cache_read_input_tokens": c.get("cache_read_input_tokens", 0)
        + extracted["cache_read_input_tokens"],
    }
