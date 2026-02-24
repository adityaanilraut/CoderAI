"""Event emitter for decoupling core logic from UI."""

import asyncio
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

    def off(self, event: str, callback: Callable) -> None:
        """Unsubscribe from an event.

        Args:
            event: Event name
            callback: Function or coroutine to remove
        """
        if event in self._listeners and callback in self._listeners[event]:
            self._listeners[event].remove(callback)

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
                if asyncio.iscoroutinefunction(callback):
                    # For coroutines, create a task so it doesn't block
                    asyncio.create_task(callback(*args, **kwargs))
                else:
                    callback(*args, **kwargs)
            except Exception as e:
                # Log or handle callback errors without breaking the emitter
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f"Error in event listener for '{event}': {e}")


# Global event emitter instance for app-wide events
event_emitter = EventEmitter()
