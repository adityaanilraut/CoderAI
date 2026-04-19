"""Event emitter for decoupling core logic from UI."""

import asyncio
import inspect
from typing import Any, Callable, Dict, List


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

                        # Log exceptions from fire-and-forget tasks so they
                        # don't vanish silently.
                        def _on_task_done(t, _event=event, _cb=callback):
                            if t.cancelled():
                                return
                            exc = t.exception()
                            if exc is not None:
                                import logging
                                _logger = logging.getLogger(__name__)
                                _logger.error(
                                    "Unhandled error in async listener for '%s' (%s): %s",
                                    _event, _cb, exc,
                                )
                        task.add_done_callback(_on_task_done)
                    except RuntimeError:
                        # No running event loop — run the coroutine synchronously
                        asyncio.run(callback(*args, **kwargs))
                else:
                    callback(*args, **kwargs)
            except Exception as e:
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f"Error in event listener for '{event}': {e}")


# Global event emitter instance for app-wide events
event_emitter = EventEmitter()
