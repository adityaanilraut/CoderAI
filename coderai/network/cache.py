"""TTL Response Cache for Web Search and Fetch operations."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from dataclasses import dataclass
from typing import Any


def normalize_search_query(query: str) -> str:
    """Collapse case/whitespace variants to one canonical form."""
    return " ".join(query.strip().split()).casefold()


def build_search_key(provider: str, query: str, max_results: int) -> str:
    """Build a provider-scoped search cache key.

    The key honors the result-count parameter so a truncated cached answer is
    never served to a caller that asked for more results.
    """
    return f"search:{provider.strip().lower()}:{normalize_search_query(query)}:{int(max_results)}"


@dataclass
class CacheEntry:
    key: str
    value: Any
    created_at: float
    expires_at: float
    metadata: dict[str, Any] | None = None

    def is_expired(self) -> bool:
        return time.time() > self.expires_at


class ResponseCache:
    """Thread-safe in-memory TTL response cache with LRU eviction.

    Dict insertion order tracks recency: hits move entries to the back and
    eviction takes from the front, so hot keys survive cold-insert churn.
    """

    def __init__(self, default_ttl_seconds: float = 300.0, max_entries: int = 500) -> None:
        self.default_ttl = default_ttl_seconds
        self.max_entries = max_entries
        self._cache: dict[str, CacheEntry] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def _generate_key(self, prefix: str, payload: Any) -> str:
        if isinstance(payload, str):
            serialized = payload
        else:
            try:
                serialized = json.dumps(payload, sort_keys=True)
            except Exception:
                serialized = str(payload)
        h = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
        return f"{prefix}:{h}"

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self.misses += 1
                return None

            if entry.is_expired():
                del self._cache[key]
                self.misses += 1
                return None

            self.hits += 1
            # LRU: refresh recency and hand out a copy so callers can neither
            # mislabel (from_cache flips) nor poison the stored value.
            self._cache[key] = self._cache.pop(key)
            return copy.deepcopy(entry.value)

    def set(
        self,
        key: str,
        value: Any,
        ttl_seconds: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl
        now = time.time()
        entry = CacheEntry(
            key=key,
            # Store a copy so later caller-side mutation cannot poison the cache.
            value=copy.deepcopy(value),
            created_at=now,
            expires_at=now + ttl,
            metadata=metadata,
        )

        with self._lock:
            # Evict expired entries if approaching max_entries
            if len(self._cache) >= self.max_entries:
                expired_keys = [k for k, v in self._cache.items() if v.is_expired()]
                for k in expired_keys:
                    del self._cache[k]

                # If still at or over capacity, evict least-recently-used entry
                if len(self._cache) >= self.max_entries and key not in self._cache:
                    lru_key = next(iter(self._cache))
                    del self._cache[lru_key]

            self._cache[key] = entry

    def delete(self, key: str) -> bool:
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self.hits = 0
            self.misses = 0

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "size": len(self._cache),
                "maxEntries": self.max_entries,
                "hits": self.hits,
                "misses": self.misses,
                "hitRate": (self.hits / (self.hits + self.misses))
                if (self.hits + self.misses) > 0
                else 0.0,
            }


# Global shared cache instances
_web_search_cache = ResponseCache(default_ttl_seconds=600.0, max_entries=300)
_web_fetch_cache = ResponseCache(default_ttl_seconds=300.0, max_entries=200)


def get_search_cache() -> ResponseCache:
    return _web_search_cache


def get_fetch_cache() -> ResponseCache:
    return _web_fetch_cache
