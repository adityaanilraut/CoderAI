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

# The cache is never pruned on read for entries that are simply never read
# again, so it can grow without bound. Cap the number of entries and prune
# expired files opportunistically (throttled) whenever we write.
_MAX_CACHE_ENTRIES = 1000
_PRUNE_INTERVAL = 60.0  # min seconds between opportunistic prunes
_last_prune: float = 0.0


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
    _maybe_prune()


def _prune_cache(max_entries: int = _MAX_CACHE_ENTRIES) -> int:
    """Drop expired/corrupt cache files and cap the directory to *max_entries*.

    Expired and unparseable files are removed first; if more than *max_entries*
    live entries remain, the oldest (by mtime) are evicted. Best-effort: I/O
    errors on individual files are skipped, never raised. Returns the number of
    files removed.
    """
    try:
        files = list(_CACHE_DIR.glob("*.json"))
    except OSError:
        return 0

    now = time.time()
    removed = 0
    survivors: list[tuple[float, Path]] = []  # (mtime, path) for live entries
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            expires = data.get("expires", 0)
        except (json.JSONDecodeError, OSError):
            path.unlink(missing_ok=True)
            removed += 1
            continue
        if expires < now:
            path.unlink(missing_ok=True)
            removed += 1
            continue
        try:
            survivors.append((path.stat().st_mtime, path))
        except OSError:
            continue

    if len(survivors) > max_entries:
        survivors.sort()  # oldest mtime first
        for _mtime, path in survivors[: len(survivors) - max_entries]:
            try:
                path.unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass

    return removed


def _maybe_prune() -> None:
    """Run :func:`_prune_cache` at most once per ``_PRUNE_INTERVAL`` seconds."""
    global _last_prune
    now = time.monotonic()
    if now - _last_prune < _PRUNE_INTERVAL:
        return
    _last_prune = now
    try:
        removed = _prune_cache()
        if removed:
            logger.debug("Pruned %d expired/excess cache files", removed)
    except Exception:
        logger.debug("Cache prune failed", exc_info=True)
