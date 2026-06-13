"""Disk cache for search results and fetched page content."""

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_CACHE_DIR = Path.home() / ".coderAI" / "cache"
_DEFAULT_SEARCH_TTL = 300
_DEFAULT_PAGE_TTL = 3600


def _cache_dir() -> Path:
    # 0700: cached search queries / page content can be sensitive
    _CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    return _CACHE_DIR


def _cache_key(prefix: str, *parts: str) -> str:
    raw = "|".join((prefix,) + parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _cache_path(key: str) -> Path:
    return _cache_dir() / f"{key}.json"


def _get_cached(key: str) -> Optional[Any]:
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("expires", 0) < time.time():
            path.unlink(missing_ok=True)
            return None
        return data.get("value")
    except (json.JSONDecodeError, OSError):
        path.unlink(missing_ok=True)
        return None


def _set_cached(key: str, value: Any, ttl: int) -> None:
    try:
        data = {
            "value": value,
            "expires": time.time() + ttl,
            "cached_at": time.time(),
        }
        path = _cache_path(key)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        if os.name != "nt":
            os.chmod(path, 0o600)
    except OSError as e:
        logger.debug(f"Cache write failed for {key}: {e}")
