"""Anthropic token counting with caching and char/4 fallback.

Uses ``requests`` (declared dependency) for the HTTP call. When called from
inside a running asyncio event loop, the function falls back to the
character-count heuristic rather than blocking the loop with a synchronous
HTTP call.  The per-fingerprint cache ensures exact counts obtained during
asyncio-safe invocations are reused.
"""

from __future__ import annotations
import asyncio
import hashlib
import logging
import threading
from collections import OrderedDict
from typing import Optional, Tuple
import requests

logger = logging.getLogger(__name__)

_TOKENS_PER_CHAR_FALLBACK = 4
_CACHE_MAX = 1024

_cache: "OrderedDict[Tuple[str, str], int]" = OrderedDict()
_cache_lock = threading.Lock()


def estimate_chars(text: str) -> int:
    from coderAI.llm.base import estimate_tokens_by_chars

    return estimate_tokens_by_chars(text)


def _do_count_tokens_request(text: str, model: str, api_key: str) -> int:
    """Synchronous HTTP call to the Anthropic count_tokens endpoint."""
    r = requests.post(
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
    return int(r.json().get("input_tokens", 0))


def count_tokens_anthropic(text: str, model: str, api_key: Optional[str]) -> int:
    """Count tokens via Anthropic count_tokens endpoint, fall back to char/4.

    When called from inside a running asyncio event loop the function falls
    back to the character-count heuristic to avoid blocking the loop with a
    synchronous HTTP call.  The per-fingerprint cache ensures that any exact
    count obtained during a non-async invocation is reused on later calls.
    """
    if not api_key:
        return estimate_chars(text)
    digest = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:16]
    cache_key = (model, digest)
    with _cache_lock:
        if cache_key in _cache:
            _cache.move_to_end(cache_key)
            return _cache[cache_key]
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        return estimate_chars(text)
    try:
        n = _do_count_tokens_request(text, model, api_key)
        if n > 0:
            with _cache_lock:
                _cache[cache_key] = n
                if len(_cache) > _CACHE_MAX:
                    _cache.popitem(last=False)
            return n
    except Exception:
        logger.debug("Anthropic token count fallback — using char/4 estimate")
    return estimate_chars(text)
