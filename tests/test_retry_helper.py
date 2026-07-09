"""Tests for coderAI.system.retry — canonical backoff + generic async retry."""

import asyncio

import pytest

from coderAI.llm.base import _exponential_backoff_sleep
from coderAI.system.retry import backoff_delay, retry_async


class TestBackoffDelay:
    def test_exponential_growth_without_jitter(self, monkeypatch):
        monkeypatch.setattr("coderAI.system.retry.random.uniform", lambda a, b: 0.0)
        assert backoff_delay(1, base=1.0, cap=60.0) == 1.0
        assert backoff_delay(2, base=1.0, cap=60.0) == 2.0
        assert backoff_delay(3, base=1.0, cap=60.0) == 4.0
        assert backoff_delay(4, base=0.5, cap=60.0) == 4.0

    def test_cap_bounds_growth(self, monkeypatch):
        monkeypatch.setattr("coderAI.system.retry.random.uniform", lambda a, b: 0.0)
        assert backoff_delay(10, base=1.0, cap=8.0) == 8.0

    def test_jitter_bounds(self):
        for attempt in (1, 3, 7):
            expected = min(1.0 * (2 ** (attempt - 1)), 8.0)
            for _ in range(50):
                delay = backoff_delay(attempt, base=1.0, cap=8.0, jitter=0.3)
                assert expected <= delay <= expected * 1.3 + 1e-9

    def test_llm_delegate_keeps_historical_bounds(self):
        """llm/base.py's helper now delegates here; pin its historic envelope
        (base=1.0, factor=2, cap=8.0, jitter=0.3) so provider retry pacing
        is unchanged."""
        for attempt in (1, 2, 3, 4, 5):
            expected = min(2 ** (attempt - 1), 8.0)
            delay = _exponential_backoff_sleep(attempt)
            assert expected <= delay <= expected * 1.3 + 1e-9


class TestRetryAsync:
    async def test_transient_failure_then_success(self):
        calls = []

        async def fn():
            calls.append(1)
            if len(calls) < 2:
                raise RuntimeError("connection reset")
            return "ok"

        result = await retry_async(
            fn, max_retries=2, is_retryable=lambda e: True, base_delay=0.0
        )
        assert result == "ok"
        assert len(calls) == 2

    async def test_non_retryable_raises_immediately(self):
        calls = []

        async def fn():
            calls.append(1)
            raise ValueError("bad input")

        with pytest.raises(ValueError):
            await retry_async(fn, max_retries=3, is_retryable=lambda e: False, base_delay=0.0)
        assert len(calls) == 1

    async def test_exhaustion_raises_last_failure(self):
        calls = []

        async def fn():
            calls.append(1)
            raise RuntimeError(f"attempt {len(calls)}")

        with pytest.raises(RuntimeError, match="attempt 3"):
            await retry_async(fn, max_retries=2, is_retryable=lambda e: True, base_delay=0.0)
        assert len(calls) == 3

    async def test_cancel_event_stops_retries(self):
        cancel = asyncio.Event()
        cancel.set()
        calls = []

        async def fn():
            calls.append(1)
            raise RuntimeError("connection reset")

        with pytest.raises(RuntimeError):
            await retry_async(
                fn,
                max_retries=3,
                is_retryable=lambda e: True,
                base_delay=0.0,
                cancel_event=cancel,
            )
        assert len(calls) == 1
