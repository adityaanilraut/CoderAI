"""Async-safe SIGINT handler — mirrors Kimi CLI utils/signals.py (std lib only)."""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Callable


def install_sigint_handler(
    loop: asyncio.AbstractEventLoop, handler: Callable[[], None]
) -> Callable[[], None]:
    """Install SIGINT that works on Unix and Windows.

    Prefers loop.add_signal_handler; falls back to signal.signal on
    Windows ProactorEventLoop. Returns a remover that never raises.
    """
    try:
        loop.add_signal_handler(signal.SIGINT, handler)

        def remove() -> None:
            with contextlib.suppress(RuntimeError):
                loop.remove_signal_handler(signal.SIGINT)

        return remove
    except (RuntimeError, NotImplementedError, ValueError):
        previous = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, lambda _s, _f: handler())

        def remove() -> None:
            with contextlib.suppress(RuntimeError, ValueError):
                signal.signal(signal.SIGINT, previous)

        return remove
