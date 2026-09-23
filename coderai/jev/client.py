"""Async, timeout-bounded, cached wrapper around TriageEngine (Jev System-One).

Jev is non-autoregressive: single-shot `system_one(state, questions)` with no
tools and no streaming. Every entrypoint here fails safe to the same
fallbacks as the sync engine so callers can always degrade to the
System-Two autoregressive loop:

- Tier 1 triage failure -> should_review=True (fail-closed, never skip review)
- Tier 3 gate failure   -> passed=True (fail-open, never drop a comment silently)
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable, Sequence
from functools import partial
from typing import Any

from coderai.triage.engine import (
    JEV_DEFAULT_TIMEOUT_S,
    GateResult,
    TriageResult,
    get_triage_engine,
    jev_accept_threshold,
    jev_gate_threshold,
    jev_min_confidence,
    jev_sdk_installed,
    jev_speculative_threshold,
    jev_triage_threshold,
    truncate_diff,
)

_DEFAULT_CACHE_SIZE = 512

_triage_cache: OrderedDict[str, TriageResult] = OrderedDict()
_gate_cache: OrderedDict[str, GateResult] = OrderedDict()
_cache_lock = threading.Lock()
_cache_counters = {"triage_hits": 0, "triage_misses": 0, "gate_hits": 0, "gate_misses": 0}


def jev_cache_size() -> int:
    """LRU capacity, overridable via CODERAI_JEV_CACHE_SIZE."""
    try:
        raw = os.getenv("CODERAI_JEV_CACHE_SIZE")
        if raw is not None and raw.strip():
            return max(16, int(float(raw.strip())))
    except (TypeError, ValueError, AttributeError):
        pass
    return _DEFAULT_CACHE_SIZE


def _cache_get(cache: OrderedDict, key: str) -> Any | None:
    with _cache_lock:
        try:
            val = cache.pop(key)
            cache[key] = val  # re-insert as most-recent
            return val
        except KeyError:
            return None


def _cache_put(cache: OrderedDict, key: str, val: Any) -> None:
    cap = jev_cache_size()
    with _cache_lock:
        cache[key] = val
        while len(cache) > cap:
            cache.popitem(last=False)


def _record_lookup(tier: str, hit: bool) -> None:
    with _cache_lock:
        _cache_counters[f"{tier}_{'hits' if hit else 'misses'}"] += 1


def _key_id(api_key: str | None) -> str:
    # Hash, never store the raw key.
    return hashlib.sha256((api_key or "local").encode("utf-8", "ignore")).hexdigest()[:8]


# Prefixes whose results must never be cached (negative-cache poisoning).
# Successes and the deterministic SDK-missing local heuristics
# ("Fallback heuristic", "Fallback: TypeSafe") remain cacheable; backend
# identity in the key already separates those.
_FALLBACK_NO_CACHE_PREFIXES = (
    "Jev async fallback",
    "Jev async gate fallback",
    "Triage error fallback",
    "Gate error fallback",
)


def _is_failure_fallback(reason: str) -> bool:
    return reason.startswith(_FALLBACK_NO_CACHE_PREFIXES)


def _triage_key(
    file_path: str,
    diff: str,
    threshold: float,
    available: bool,
    api_key: str | None = None,
) -> str:
    # Key on the truncated payload (the bytes the model actually sees) plus
    # threshold and backend identity (local-heuristic vs live SDK) so a
    # threshold change or SDK install never serves a stale verdict.
    h = hashlib.sha256(truncate_diff(diff).encode("utf-8", "ignore")).hexdigest()[:16]
    return (
        f"{file_path}\x00{repr(float(threshold))}\x00{repr(jev_min_confidence())}"
        f"\x00{int(available)}\x00{h}\x00{_key_id(api_key)}"
    )


def _gate_key(
    file_path: str,
    diff: str,
    comment: str,
    threshold: float,
    available: bool,
    accept: float | None = None,
    speculative: float | None = None,
    api_key: str | None = None,
) -> str:
    if accept is None:
        accept = jev_accept_threshold()
    if speculative is None:
        speculative = jev_speculative_threshold()
    h = hashlib.sha256(
        (truncate_diff(diff) + "\x00" + comment).encode("utf-8", "ignore")
    ).hexdigest()[:16]
    return (
        f"{file_path}\x00{repr(float(threshold))}\x00{repr(float(accept))}"
        f"\x00{repr(float(speculative))}\x00{int(available)}\x00{h}\x00{_key_id(api_key)}"
    )


def jev_timeout_s() -> float:
    """Per-call timeout (seconds), overridable via CODERAI_JEV_TIMEOUT_S."""
    try:
        return max(0.5, float(os.getenv("CODERAI_JEV_TIMEOUT_S", str(JEV_DEFAULT_TIMEOUT_S))))
    except (TypeError, ValueError):
        return JEV_DEFAULT_TIMEOUT_S


def jev_max_workers() -> int:
    """Bounded worker pool size, overridable via CODERAI_JEV_MAX_WORKERS."""
    try:
        raw = os.getenv("CODERAI_JEV_MAX_WORKERS")
        if raw is not None and raw.strip():
            return max(1, min(32, int(float(raw.strip()))))
    except (TypeError, ValueError, AttributeError):
        pass
    return 8


_executor_lock = threading.Lock()
_jev_executor: ThreadPoolExecutor | None = None


def _get_executor() -> ThreadPoolExecutor:
    global _jev_executor
    with _executor_lock:
        if _jev_executor is None:
            _jev_executor = ThreadPoolExecutor(
                max_workers=jev_max_workers(), thread_name_prefix="jev-triage"
            )
        return _jev_executor


def _reset_executor_for_tests() -> None:
    global _jev_executor
    with _executor_lock:
        if _jev_executor is not None:
            _jev_executor.shutdown(wait=False, cancel_futures=True)
            _jev_executor = None


def jev_is_available(api_key: str | None = None) -> bool:
    """True if the Jev backend can be reached (SDK installed + key present)."""
    try:
        return get_triage_engine(api_key=api_key).is_available
    except Exception:
        return False


async def _run_bounded(fn: Callable[[], Any], timeout: float) -> Any:
    """Run ``fn`` in the Jev pool with ``timeout`` measured from when it starts running.

    Time spent queued behind other calls gets its own ``timeout`` budget; a job still
    queued when that expires is cancelled before it runs, so a burst of slow calls
    cannot snowball into timeouts for work that never started.
    """
    loop = asyncio.get_running_loop()
    started = asyncio.Event()

    def _job() -> Any:
        try:
            loop.call_soon_threadsafe(started.set)
        except RuntimeError:
            pass
        return fn()

    fut = loop.run_in_executor(_get_executor(), _job)
    try:
        await asyncio.wait_for(started.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        fut.cancel()
        raise
    return await asyncio.wait_for(fut, timeout=timeout)


def _log_timeout(
    tier: str,
    file_path: str,
    elapsed_s: float,
    timeout: float,
    is_timeout: bool = False,
) -> None:
    try:
        from coderai.utils.logging import logger

        kind = "timeout" if is_timeout else "error"
        logger.warning(
            "Jev %s %s: file=%s elapsed=%.2fs timeout=%.2fs "
            "(worker thread continues in bounded pool; fail-safe returned)",
            tier,
            kind,
            file_path,
            elapsed_s,
            timeout,
        )
    except Exception:
        pass


async def jev_screen_diff_async(
    file_path: str,
    diff_hunk: str,
    *,
    api_key: str | None = None,
    timeout_s: float | None = None,
    use_cache: bool = True,
    threshold: float | None = None,
) -> TriageResult:
    """Tier 1 async triage. Never raises — falls back to should_review=True."""
    engine = get_triage_engine(api_key=api_key)
    cutoff = jev_triage_threshold(threshold)
    key = _triage_key(file_path, diff_hunk, cutoff, engine.is_available, engine.api_key)
    if use_cache:
        hit = _cache_get(_triage_cache, key)
        _record_lookup("triage", hit is not None)
        if hit is not None:
            return hit
    timeout = timeout_s if timeout_s is not None else jev_timeout_s()
    start = time.monotonic()
    try:
        result = await _run_bounded(
            partial(
                engine.screen_diff_hunk,
                file_path,
                diff_hunk,
                threshold=threshold,
                timeout_s=timeout,
            ),
            timeout,
        )
    except Exception as exc:
        elapsed = time.monotonic() - start
        _log_timeout(
            "triage",
            file_path,
            elapsed,
            timeout,
            is_timeout=isinstance(exc, asyncio.TimeoutError),
        )
        result = TriageResult(
            file_path=file_path,
            risk=0.50,
            category="core_logic",
            priority=1,
            should_review=True,
            reason=f"Jev async fallback: {type(exc).__name__}: {exc}",
        )
    if use_cache and not _is_failure_fallback(result.reason):
        _cache_put(_triage_cache, key, result)
    return result


async def jev_gate_comment_async(
    file_path: str,
    diff_hunk: str,
    comment: str,
    threshold: float | None = None,
    *,
    api_key: str | None = None,
    timeout_s: float | None = None,
    use_cache: bool = True,
) -> GateResult:
    """Tier 3 async precision gate. Never raises — falls back to passed=True."""
    engine = get_triage_engine(api_key=api_key)
    cutoff = jev_gate_threshold(threshold)
    key = _gate_key(
        file_path,
        diff_hunk,
        comment,
        cutoff,
        engine.is_available,
        jev_accept_threshold(),
        jev_speculative_threshold(),
        engine.api_key,
    )
    if use_cache:
        hit = _cache_get(_gate_cache, key)
        _record_lookup("gate", hit is not None)
        if hit is not None:
            return hit
    timeout = timeout_s if timeout_s is not None else jev_timeout_s()
    start = time.monotonic()
    try:
        result = await _run_bounded(
            partial(
                engine.gate_candidate_comment,
                file_path,
                diff_hunk,
                comment,
                threshold=threshold,
                timeout_s=timeout,
            ),
            timeout,
        )
    except Exception as exc:
        elapsed = time.monotonic() - start
        _log_timeout(
            "gate",
            file_path,
            elapsed,
            timeout,
            is_timeout=isinstance(exc, asyncio.TimeoutError),
        )
        result = GateResult(
            is_actionable_bug=0.80,
            will_developer_accept=0.80,
            passed=True,
            reason=f"Jev async gate fallback: {type(exc).__name__}: {exc}",
        )
    if use_cache and not _is_failure_fallback(result.reason):
        _cache_put(_gate_cache, key, result)
    return result


async def jev_screen_many_async(
    items: Sequence[tuple[str, str]],
    *,
    api_key: str | None = None,
    timeout_s: float | None = None,
    use_cache: bool = True,
    threshold: float | None = None,
    max_concurrency: int | None = None,
) -> list[TriageResult]:
    """Fan out Tier-1 triage over many (file_path, diff) pairs.

    Bounded by a semaphore (default 8) so multi-file diffs parallelize without
    overwhelming the bounded worker pool. Order of results matches input order.
    Never raises — each item falls back to should_review=True on error.
    """
    limit = max_concurrency if max_concurrency is not None else jev_max_workers()
    sem = asyncio.Semaphore(max(1, limit))

    async def _one(pair: tuple[str, str]) -> TriageResult:
        fp, diff = pair
        async with sem:
            return await jev_screen_diff_async(
                fp,
                diff,
                api_key=api_key,
                timeout_s=timeout_s,
                use_cache=use_cache,
                threshold=threshold,
            )

    return list(await asyncio.gather(*(_one(p) for p in items)))


def jev_cache_stats() -> dict[str, int | float]:
    """Cache sizes + hit counters for /config /doctor diagnostics."""
    with _cache_lock:
        return {
            "triage_cached": len(_triage_cache),
            "gate_cached": len(_gate_cache),
            "triage_hits": _cache_counters["triage_hits"],
            "triage_misses": _cache_counters["triage_misses"],
            "gate_hits": _cache_counters["gate_hits"],
            "gate_misses": _cache_counters["gate_misses"],
        }


def jev_cache_clear() -> None:
    """Clear both caches and reset hit counters (tests, /reload)."""
    with _cache_lock:
        _triage_cache.clear()
        _gate_cache.clear()
        for k in _cache_counters:
            _cache_counters[k] = 0


_STATUS_TTL_S = 30.0
_status_lock = threading.Lock()
_status_cache: dict[str, Any] = {"ts": 0.0, "payload": None, "key_hash": None}


def _status_key_hash(key: str | None) -> str:
    return hashlib.sha256((key or "").encode("utf-8", "ignore")).hexdigest()[:16]


def jev_status_ttl_s() -> float:
    """Probe TTL (seconds), overridable via CODERAI_JEV_STATUS_TTL_S."""
    try:
        raw = os.getenv("CODERAI_JEV_STATUS_TTL_S")
        if raw is not None and raw.strip():
            return max(0.0, float(raw.strip()))
    except (TypeError, ValueError, AttributeError):
        pass
    return _STATUS_TTL_S


def jev_probe(api_key: str | None = None) -> tuple[bool, float | None, str | None]:
    """One bounded live call: (reachable, latency_ms, error). Never raises or hangs."""
    if not jev_sdk_installed():
        return False, None, "typesafe-sdk is not installed (pip install 'coderai-agent[jev]')"
    try:
        engine = get_triage_engine(api_key=api_key)
    except Exception as exc:
        return False, None, f"{type(exc).__name__}: {exc}"
    if not engine.is_available:
        return False, None, "no API key (TYPESAFE_API_KEY)"
    start = time.monotonic()
    fut = _get_executor().submit(engine.screen_diff_hunk, "probe.py", "x = 1\n")
    try:
        probe = fut.result(timeout=jev_timeout_s())
    except Exception as exc:
        fut.cancel()
        return False, None, f"no response within {jev_timeout_s():.1f}s ({type(exc).__name__})"
    if _is_failure_fallback(probe.reason):
        return False, None, probe.reason
    return True, (time.monotonic() - start) * 1000.0, None


def jev_status(*, use_cache: bool = True) -> dict[str, Any]:
    """Snapshot for setup/status UI: configured, reachable, latency probe.

    The live probe is TTL-cached (default 30s) and runs in a bounded worker
    with a timeout, so /doctor and provider probing never hang without
    key/network.
    """
    from coderai.triage.engine import resolve_jev_api_key

    key = resolve_jev_api_key()
    cur_hash = _status_key_hash(key)
    if use_cache:
        ttl = jev_status_ttl_s()
        with _status_lock:
            cached = _status_cache.get("payload")
            age = time.monotonic() - float(_status_cache.get("ts") or 0.0)
            if cached is not None and age < ttl and _status_cache.get("key_hash") == cur_hash:
                return dict(cached)

    configured = bool(key)
    sdk_installed = jev_sdk_installed()
    available = False
    latency_ms: float | None = None
    error: str | None = None
    if configured:
        available, latency_ms, error = jev_probe()
    from coderai.config import mask_api_key

    payload: dict[str, Any] = {
        "model": "jev-system-one",
        "env_var": "TYPESAFE_API_KEY",
        "configured": configured,
        "sdk_installed": sdk_installed,
        "available": available,
        "masked_key": mask_api_key(key) if configured else "Not configured",
        "latency_ms": latency_ms,
        "error": error,
        "timeout_s": jev_timeout_s(),
        **jev_cache_stats(),  # type: ignore[arg-type]
    }
    with _status_lock:
        _status_cache["ts"] = time.monotonic()
        _status_cache["payload"] = dict(payload)
        _status_cache["key_hash"] = cur_hash
    return payload


def jev_status_clear() -> None:
    """Invalidate the TTL-cached status probe (mainly for tests)."""
    with _status_lock:
        _status_cache["ts"] = 0.0
        _status_cache["payload"] = None
        _status_cache["key_hash"] = None
