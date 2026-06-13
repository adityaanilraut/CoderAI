"""Per-domain rate limiting for outbound web requests."""

import asyncio
import logging
import os
import time
from collections import OrderedDict
from typing import Optional

logger = logging.getLogger(__name__)

# Cap how many distinct domains we remember the last-request time for. Without
# a bound this dict grows once per domain ever contacted for the life of the
# process; an LRU cap keeps it small while still rate-limiting the domains a
# session actually hits repeatedly.
_MAX_TRACKED_DOMAINS = 2048
_last_request: "OrderedDict[str, float]" = OrderedDict()
_rate_limit_delay: float = 1.0


def _get_rate_limit_delay() -> float:
    global _rate_limit_delay
    try:
        from coderAI.core.services import get_services

        _rate_limit_delay = get_services().config.rate_limit_delay_seconds
    except Exception:
        # Config can be unreadable (corrupt file, early startup, tests);
        # fall back to the env var / previous value instead of failing the request.
        logger.debug("rate_limit_delay config unavailable, using fallback", exc_info=True)
        env_val = os.getenv("CODERAI_RATE_LIMIT_DELAY")
        if env_val:
            try:
                _rate_limit_delay = float(env_val)
            except ValueError:
                pass
    return _rate_limit_delay


async def _rate_limit_async(hostname: Optional[str]) -> None:
    if not hostname:
        return
    delay = _get_rate_limit_delay()
    if delay <= 0:
        return
    domain = hostname.lower()
    now = time.monotonic()
    last = _last_request.get(domain, 0)
    wait = delay - (now - last)
    if wait > 0:
        logger.debug(f"Rate limiting {domain}: waiting {wait:.2f}s")
        await asyncio.sleep(wait)
    _last_request[domain] = time.monotonic()
    # Mark as most-recently-used and evict the oldest domains past the cap.
    _last_request.move_to_end(domain)
    while len(_last_request) > _MAX_TRACKED_DOMAINS:
        _last_request.popitem(last=False)
