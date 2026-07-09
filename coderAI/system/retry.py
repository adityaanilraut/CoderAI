"""Canonical exponential-backoff-with-jitter helpers.

Single home for the backoff curve that was previously hand-rolled in
``llm/base.py`` (and about to be needed by the tool executor, sub-agent
delegation, and background jobs). ``system/error_policy.py`` keeps the
*classification* side (what counts as transient); this module owns the
*pacing* side (how long to wait between attempts).
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


def backoff_delay(
    attempt: int,
    *,
    base: float = 1.0,
    factor: float = 2.0,
    cap: float = 60.0,
    jitter: float = 0.3,
) -> float:
    """Delay in seconds before retrying after failed attempt *attempt* (1-based).

    Exponential growth ``base * factor**(attempt-1)`` capped at *cap*, plus a
    uniform jitter of up to ``jitter * delay`` so simultaneous retriers don't
    stampede in lockstep. The result is therefore in ``[delay, delay*(1+jitter)]``.
    """
    delay = min(base * (factor ** (attempt - 1)), cap)
    delay += random.uniform(0, delay * jitter)
    return float(delay)


async def retry_async(
    fn: Callable[[], Awaitable[Any]],
    *,
    max_retries: int,
    is_retryable: Callable[[Exception], bool],
    base_delay: float = 1.0,
    cap: float = 8.0,
    description: str = "operation",
    cancel_event: Optional[asyncio.Event] = None,
) -> Any:
    """Await ``fn()`` with up to *max_retries* retries on retryable exceptions.

    A non-retryable exception, an exhausted budget, or a set *cancel_event*
    re-raises the last failure immediately. ``asyncio.CancelledError`` always
    propagates (it is a ``BaseException``, never swallowed here).
    """
    for attempt in range(1, max_retries + 2):
        try:
            return await fn()
        except Exception as exc:
            if attempt > max_retries or not is_retryable(exc):
                raise
            if cancel_event is not None and cancel_event.is_set():
                raise
            delay = backoff_delay(attempt, base=base_delay, cap=cap)
            logger.warning(
                "%s failed (attempt %d/%d), retrying in %.1fs: %s",
                description,
                attempt,
                max_retries + 1,
                delay,
                exc,
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover
