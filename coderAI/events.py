"""Event emitter for decoupling core logic from UI."""

import asyncio
import inspect
import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class EventEmitter:
    """Simple event emitter for Pub/Sub architecture."""

    def __init__(self):
        """Initialize event emitter."""
        self._listeners: Dict[str, List[Callable]] = {}

    def on(self, event: str, callback: Callable) -> None:
        """Subscribe to an event.

        Args:
            event: Event name
            callback: Function or coroutine to call when event occurs
        """
        if event not in self._listeners:
            self._listeners[event] = []
        if callback not in self._listeners[event]:
            self._listeners[event].append(callback)

    def off(self, event: str, callback: Callable) -> None:
        """Unsubscribe a single callback from an event (no-op if missing)."""
        listeners = self._listeners.get(event)
        if not listeners:
            return
        try:
            listeners.remove(callback)
        except ValueError:
            return
        if not listeners:
            del self._listeners[event]

    def remove_all_listeners(self, event: Optional[str] = None) -> None:
        """Drop all listeners for *event*, or every event if *event* is None.

        Use this to prevent stale listeners from leaking across session or
        process boundaries (each IPC session installs its own set).
        """
        if event is None:
            self._listeners.clear()
        else:
            self._listeners.pop(event, None)

    def emit(self, event: str, *args: Any, **kwargs: Any) -> None:
        """Emit an event to all subscribers.

        Args:
            event: Event name
            *args: Positional arguments for callbacks
            **kwargs: Keyword arguments for callbacks
        """
        if event not in self._listeners:
            return

        for callback in self._listeners[event]:
            try:
                if inspect.iscoroutinefunction(callback):
                    try:
                        loop = asyncio.get_running_loop()
                        task = loop.create_task(callback(*args, **kwargs))

                        def _on_task_done(t, _event=event, _cb=callback):
                            if t.cancelled():
                                return
                            exc = t.exception()
                            if exc is not None:
                                logger.error(
                                    "Unhandled error in async listener for '%s' (%s): %s",
                                    _event, _cb, exc,
                                )

                        task.add_done_callback(_on_task_done)
                    except RuntimeError:
                        # No running event loop — cannot dispatch async listener
                        logger.debug(
                            "Skipping async listener for '%s' — no running event loop", event
                        )
                else:
                    callback(*args, **kwargs)
            except Exception:
                logger.error("Error in event listener for '%s'", event, exc_info=True)


# Global event emitter instance for app-wide events
event_emitter = EventEmitter()
