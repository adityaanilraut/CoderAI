"""Disk cache for search results and fetched page content."""

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

from coderAI.system.fsperms import atomic_write_text

logger = logging.getLogger(__name__)

_CACHE_DIR = Path.home() / ".coderAI" / "cache"
_DEFAULT_SEARCH_TTL = 300
_DEFAULT_PAGE_TTL = 3600

# The cache is never pruned on read for entries that are simply never read
# again, so every write enforces both count and total-byte caps.
_MAX_CACHE_ENTRIES = 1000
_MAX_CACHE_BYTES = 50 * 1024 * 1024
_cache_lock = threading.RLock()


def _cache_dir() -> Path:
    # 0700: cached search queries / page content can be sensitive
    # Sandbox may deny ~/.coderAI even when the dir already exists (mkdir
    # with exist_ok succeeds but subsequent file writes fail). Probe writability.
    import tempfile

    fallback = Path(tempfile.gettempdir()) / ".coderAI_test" / "cache"
    for candidate in (_CACHE_DIR, fallback):
        try:
            candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Probe writability with a temp file.
            probe = candidate / ".writability_probe"
            try:
                probe.touch(exist_ok=True)
                probe.unlink(missing_ok=True)
            except OSError:
                continue
            return candidate
        except OSError:
            continue
    # Last resort: return fallback even if probe failed; callers handle OSError.
    return fallback


def _cache_key(prefix: str, *parts: str) -> str:
    raw = "|".join((prefix,) + parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _cache_path(key: str) -> Path:
    return _cache_dir() / f"{key}.json"


def _get_cached(key: str) -> Optional[Any]:
    with _cache_lock:
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
    with _cache_lock:
        try:
            data = {
                "value": value,
                "expires": time.time() + ttl,
                "cached_at": time.time(),
            }
            path = _cache_path(key)
            atomic_write_text(path, json.dumps(data, ensure_ascii=False))
        except (OSError, TypeError) as e:
            logger.debug("Cache write failed for %s: %s", key, e)
        try:
            removed = _prune_cache()
            if removed:
                logger.debug("Pruned %d expired/excess cache files", removed)
        except Exception:
            logger.debug("Cache prune failed", exc_info=True)


def _prune_cache(
    max_entries: int = _MAX_CACHE_ENTRIES,
    max_bytes: int = _MAX_CACHE_BYTES,
) -> int:
    """Drop bad entries and cap the cache by entry count and total bytes.

    Expired and unparseable files are removed first; if more than *max_entries*
    live entries remain, the oldest (by mtime) are evicted. Best-effort: I/O
    errors on individual files are skipped, never raised. Returns the number of
    files removed.
    """
    with _cache_lock:
        try:
            files = list(_CACHE_DIR.glob("*.json"))
        except OSError:
            return 0

        now = time.time()
        removed = 0
        survivors: list[tuple[float, int, Path]] = []
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
                stat_result = path.stat()
                survivors.append((stat_result.st_mtime, stat_result.st_size, path))
            except OSError:
                continue

        survivors.sort()  # oldest mtime first
        total_bytes = sum(size for _mtime, size, _path in survivors)
        while survivors and (len(survivors) > max_entries or total_bytes > max_bytes):
            _mtime, size, path = survivors.pop(0)
            try:
                path.unlink(missing_ok=True)
                total_bytes -= size
                removed += 1
            except OSError:
                pass

        return removed
