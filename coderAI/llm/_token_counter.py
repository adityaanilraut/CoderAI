"""Anthropic token counting with caching, sync/async fallback to char/4."""
from __future__ import annotations
import hashlib
from functools import lru_cache
from typing import Optional
import httpx

_TOKENS_PER_CHAR_FALLBACK = 4

_cache: dict = {}

@lru_cache(maxsize=1024)
def _cached_count(model: str, text_hash: str, char_len: int) -> Optional[int]:
    # Cache only by hash; the actual API call lives in count_tokens.
    return None

def estimate_chars(text: str) -> int:
    return max(1, len(text) // _TOKENS_PER_CHAR_FALLBACK)

def count_tokens_anthropic(text: str, model: str, api_key: Optional[str]) -> int:
    """Count tokens via Anthropic count_tokens endpoint, fall back to char/4."""
    if not api_key:
        return estimate_chars(text)
    digest = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:16]
    cache_key = (model, digest)
    if cache_key in _cache:
        return _cache[cache_key]
    try:
        r = httpx.post(
            "https://api.anthropic.com/v1/messages/count_tokens",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={"model": model, "messages": [{"role": "user", "content": text}]},
            timeout=10.0,
        )
        r.raise_for_status()
        n = int(r.json().get("input_tokens", 0))
        if n > 0:
            _cache[cache_key] = n
            return n
    except Exception:
        pass
    return estimate_chars(text)
