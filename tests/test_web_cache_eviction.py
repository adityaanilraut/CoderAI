"""Tests for Phase 5.3 bounded growth: rate-limit LRU cap + cache eviction."""

import json
import os
import time

import pytest

from coderAI.tools.web import _cache as cache_mod
from coderAI.tools.web import _ratelimit as ratelimit_mod


# ---------------------------------------------------------------------------
# Rate-limit per-domain dict is LRU-bounded
# ---------------------------------------------------------------------------


class TestRateLimitLruCap:
    @pytest.fixture(autouse=True)
    def _clean_state(self, monkeypatch):
        ratelimit_mod._last_request.clear()
        # Positive delay keeps the recording branch live; fresh domains have
        # last=0 so the computed wait is negative and nothing sleeps.
        monkeypatch.setattr(ratelimit_mod, "_get_rate_limit_delay", lambda: 1.0)
        yield
        ratelimit_mod._last_request.clear()

    @pytest.mark.asyncio
    async def test_dict_is_capped_to_max_tracked_domains(self, monkeypatch):
        monkeypatch.setattr(ratelimit_mod, "_MAX_TRACKED_DOMAINS", 3)
        for i in range(6):
            await ratelimit_mod._rate_limit_async(f"d{i}.example.com")
        assert len(ratelimit_mod._last_request) == 3

    @pytest.mark.asyncio
    async def test_cap_evicts_oldest_keeps_most_recent(self, monkeypatch):
        monkeypatch.setattr(ratelimit_mod, "_MAX_TRACKED_DOMAINS", 3)
        for i in range(5):
            await ratelimit_mod._rate_limit_async(f"d{i}.example.com")
        # d0/d1 evicted; the three most recently seen survive.
        assert set(ratelimit_mod._last_request) == {
            "d2.example.com",
            "d3.example.com",
            "d4.example.com",
        }

    @pytest.mark.asyncio
    async def test_empty_hostname_records_nothing(self):
        await ratelimit_mod._rate_limit_async("")
        await ratelimit_mod._rate_limit_async(None)
        assert len(ratelimit_mod._last_request) == 0


# ---------------------------------------------------------------------------
# Disk cache pruning
# ---------------------------------------------------------------------------


def _write_entry(directory, name, *, expires, mtime=None):
    path = directory / f"{name}.json"
    path.write_text(
        json.dumps({"value": name, "expires": expires, "cached_at": 0}),
        encoding="utf-8",
    )
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class TestCachePrune:
    @pytest.fixture(autouse=True)
    def _tmp_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cache_mod, "_CACHE_DIR", tmp_path)
        self.cache_dir = tmp_path
        yield

    def test_removes_expired_and_corrupt_keeps_fresh(self):
        now = time.time()
        fresh = _write_entry(self.cache_dir, "fresh", expires=now + 1000)
        expired = _write_entry(self.cache_dir, "expired", expires=now - 10)
        corrupt = self.cache_dir / "corrupt.json"
        corrupt.write_text("{not valid json", encoding="utf-8")

        removed = cache_mod._prune_cache()

        assert removed == 2
        assert fresh.exists()
        assert not expired.exists()
        assert not corrupt.exists()

    def test_size_cap_evicts_oldest_by_mtime(self):
        now = time.time()
        # Five live entries with ascending mtimes; cap to 2 keeps newest two.
        for i in range(5):
            _write_entry(self.cache_dir, f"e{i}", expires=now + 1000, mtime=1000 + i)

        removed = cache_mod._prune_cache(max_entries=2)

        assert removed == 3
        survivors = {p.stem for p in self.cache_dir.glob("*.json")}
        assert survivors == {"e3", "e4"}

    def test_byte_cap_evicts_oldest(self):
        now = time.time()
        paths = [
            _write_entry(self.cache_dir, f"b{i}", expires=now + 1000, mtime=1000 + i)
            for i in range(3)
        ]
        one_entry_size = paths[0].stat().st_size

        removed = cache_mod._prune_cache(max_entries=10, max_bytes=one_entry_size * 2)

        assert removed == 1
        assert {p.stem for p in self.cache_dir.glob("*.json")} == {"b1", "b2"}

    def test_prune_on_missing_dir_is_noop(self, monkeypatch):
        monkeypatch.setattr(cache_mod, "_CACHE_DIR", self.cache_dir / "does-not-exist")
        assert cache_mod._prune_cache() == 0


class TestMaybePruneThrottle:
    def test_throttled_within_interval(self, monkeypatch):
        calls = {"n": 0}

        def _fake_prune(*a, **k):
            calls["n"] += 1
            return 0

        monkeypatch.setattr(cache_mod, "_prune_cache", _fake_prune)
        monkeypatch.setattr(cache_mod, "_PRUNE_INTERVAL", 60.0)
        # Force the throttle window open, then call twice in quick succession.
        monkeypatch.setattr(cache_mod, "_last_prune", 0.0)
        monkeypatch.setattr(cache_mod.time, "monotonic", lambda: 1000.0)

        cache_mod._maybe_prune()
        cache_mod._maybe_prune()

        assert calls["n"] == 1  # second call is throttled
