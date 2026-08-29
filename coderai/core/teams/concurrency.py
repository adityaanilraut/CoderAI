"""Optimistic concurrency control (CAS) and retry strategies with exponential backoff & jitter."""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import time
from collections.abc import Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class ConcurrencyConflictError(ValueError):
    """Raised when an optimistic concurrency check (CAS revision mismatch) fails."""

    def __init__(
        self,
        message: str,
        resource_id: str | None = None,
        expected_revision: int | None = None,
        actual_revision: int | None = None,
    ) -> None:
        super().__init__(message)
        self.resource_id = resource_id
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision


def calculate_backoff_delay(
    attempt: int,
    initial_delay: float = 0.05,
    max_delay: float = 1.0,
    factor: float = 2.0,
    jitter: bool = True,
) -> float:
    """Calculate exponential backoff with full randomized jitter."""
    calculated = min(max_delay, initial_delay * (factor**attempt))
    if jitter:
        # Full jitter: random duration between initial_delay/2 and calculated delay
        min_d = initial_delay * 0.5
        return random.uniform(min_d, calculated)
    return calculated


async def cas_retry_async(
    coroutine_fn: Callable[[], Any],
    max_retries: int = 5,
    initial_delay: float = 0.05,
    max_delay: float = 1.0,
    factor: float = 2.0,
) -> Any:
    """Execute an async operation with CAS conflict retry and exponential jitter backoff."""
    for attempt in range(max_retries):
        try:
            if inspect.iscoroutinefunction(coroutine_fn):
                return await coroutine_fn()
            res = coroutine_fn()
            if inspect.isawaitable(res):
                return await res
            return res
        except (ConcurrencyConflictError, ValueError) as exc:
            msg = str(exc)
            if "revision mismatch" not in msg.lower() and not isinstance(
                exc, ConcurrencyConflictError
            ):
                raise

            if attempt >= max_retries - 1:
                logger.error(
                    "CAS retry exceeded max retries (%d) due to persistent conflicts: %s",
                    max_retries,
                    exc,
                )
                raise

            delay = calculate_backoff_delay(
                attempt,
                initial_delay=initial_delay,
                max_delay=max_delay,
                factor=factor,
                jitter=True,
            )
            logger.debug(
                "CAS conflict on attempt %d/%d; retrying in %.3fs: %s",
                attempt + 1,
                max_retries,
                delay,
                exc,
            )
            await asyncio.sleep(delay)


def cas_retry_sync(
    fn: Callable[[], T],
    max_retries: int = 5,
    initial_delay: float = 0.05,
    max_delay: float = 1.0,
    factor: float = 2.0,
) -> T:
    """Execute a sync operation with CAS conflict retry and exponential jitter backoff."""
    for attempt in range(max_retries):
        try:
            return fn()
        except (ConcurrencyConflictError, ValueError) as exc:
            msg = str(exc)
            if "revision mismatch" not in msg.lower() and not isinstance(
                exc, ConcurrencyConflictError
            ):
                raise

            if attempt >= max_retries - 1:
                logger.error(
                    "CAS sync retry exceeded max retries (%d): %s",
                    max_retries,
                    exc,
                )
                raise

            delay = calculate_backoff_delay(
                attempt,
                initial_delay=initial_delay,
                max_delay=max_delay,
                factor=factor,
                jitter=True,
            )
            time.sleep(delay)
    raise RuntimeError("Unreachable CAS retry loop termination")
