"""Explicit service container replacing module-level tool singletons.

The container is carried in a ``ContextVar`` mirroring
``core/execution_context.py``. ``get_services()`` falls back to a
lazily-built process-wide instance, which preserves the intentional
cross-agent sharing of these services (notepad, tracker, undo, MCP) by
construction — sub-agents see the parent's container automatically because
asyncio copies contextvars into new tasks. Tests get isolation by entering
``services_scope()`` instead of monkeypatching module globals.

Tool constructors stay zero-arg (required by ``tools/discovery.py``);
tools resolve services at call time via ``get_services()``.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Callable, Iterator, Optional, TypeVar, cast

T = TypeVar("T")

if TYPE_CHECKING:
    from coderAI.core.agent_tracker import AgentTracker
    from coderAI.system.config import Config
    from coderAI.system.history import HistoryManager
    from coderAI.system.locks import ResourceManager
    from coderAI.tools.mcp import MCPClient
    from coderAI.tools.memory import MemoryStore
    from coderAI.tools.notepad import SharedNotepad
    from coderAI.tools.undo import FileBackupStore
    from coderAI.tools.web._http import HttpClient


class ToolServices:
    """Owner of the shared services tools used to reach via module globals.

    Every field is built lazily on first access so that instantiating the
    container has no side effects (no directories created, no sessions
    opened). Pass instances to the constructor to inject replacements
    (tests), or set ``parent`` to fall back to an enclosing scope for any
    field not overridden here (used to bind e.g. a per-agent config while
    still sharing the process-wide stores).
    """

    def __init__(
        self,
        *,
        parent: Optional["ToolServices"] = None,
        config: Optional["Config"] = None,
        events: Any = None,
        history: Optional["HistoryManager"] = None,
        http: Optional["HttpClient"] = None,
        memory_store: Optional["MemoryStore"] = None,
        backup_store: Optional["FileBackupStore"] = None,
        notepad: Optional["SharedNotepad"] = None,
        lock_manager: Optional["ResourceManager"] = None,
        agent_tracker: Optional["AgentTracker"] = None,
        mcp_client: Optional["MCPClient"] = None,
    ) -> None:
        self._parent = parent
        self._config = config
        self._events = events
        self._history = history
        self._http = http
        self._memory_store = memory_store
        self._backup_store = backup_store
        self._notepad = notepad
        self._lock_manager = lock_manager
        self._agent_tracker = agent_tracker
        self._mcp_client = mcp_client
        # Guards lazy builds; tool batches may resolve services from worker
        # threads (e.g. asyncio.to_thread bodies).
        self._build_lock = threading.Lock()

    def _resolve(self, field: str, build: Callable[[], T]) -> T:
        attr = f"_{field}"
        val = getattr(self, attr)
        if val is not None:
            return cast(T, val)
        if self._parent is not None:
            return cast(T, getattr(self._parent, field))
        with self._build_lock:
            val = getattr(self, attr)
            if val is None:
                val = build()
                setattr(self, attr, val)
        return cast(T, val)

    @property
    def config(self) -> "Config":
        """Effective config for tool execution.

        When unbound, this stays dynamic (re-reads ``config_manager``) rather
        than caching a snapshot, so config changes between accesses are seen
        exactly as they were with direct ``config_manager.load()`` calls.
        """
        if self._config is not None:
            return self._config
        if self._parent is not None:
            return self._parent.config
        from coderAI.system.config import config_manager

        return config_manager.load()

    @property
    def events(self) -> Any:
        """Event emitter; defaults to the process-wide UI event bus."""
        if self._events is not None:
            return self._events
        if self._parent is not None:
            return self._parent.events
        from coderAI.system.events import event_emitter

        return event_emitter

    @property
    def history(self) -> "HistoryManager":
        """Session history manager; defaults to the process-wide singleton.

        Re-reads the module singleton each access (like ``events``/``config``)
        so tests that patch ``coderAI.system.history.history_manager`` — or its
        methods — are observed by core.
        """
        if self._history is not None:
            return self._history
        if self._parent is not None:
            return self._parent.history
        from coderAI.system.history import history_manager

        return history_manager

    @property
    def memory_store(self) -> "MemoryStore":
        def _build() -> "MemoryStore":
            from coderAI.tools.memory import MemoryStore

            return MemoryStore()

        return self._resolve("memory_store", _build)

    @property
    def backup_store(self) -> "FileBackupStore":
        def _build() -> "FileBackupStore":
            from coderAI.tools.undo import FileBackupStore

            return FileBackupStore()

        return self._resolve("backup_store", _build)

    @property
    def notepad(self) -> "SharedNotepad":
        def _build() -> "SharedNotepad":
            from coderAI.tools.notepad import SharedNotepad

            return SharedNotepad()

        return self._resolve("notepad", _build)

    @property
    def lock_manager(self) -> "ResourceManager":
        def _build() -> "ResourceManager":
            from coderAI.system.locks import ResourceManager

            return ResourceManager()

        return self._resolve("lock_manager", _build)

    @property
    def agent_tracker(self) -> "AgentTracker":
        def _build() -> "AgentTracker":
            from coderAI.core.agent_tracker import AgentTracker

            return AgentTracker()

        return self._resolve("agent_tracker", _build)

    @property
    def mcp_client(self) -> "MCPClient":
        """Shared MCP client; defaults to the process-wide singleton.

        Re-reads ``coderAI.tools.mcp.mcp_client`` each access (like
        ``events``) rather than building a fresh client, so core, the tool
        router, and tests that rebind or patch that singleton all observe the
        same connected client.
        """
        if self._mcp_client is not None:
            return self._mcp_client
        if self._parent is not None:
            return self._parent.mcp_client
        from coderAI.tools.mcp import mcp_client

        return mcp_client

    @property
    def http(self) -> "HttpClient":
        def _build() -> "HttpClient":
            from coderAI.tools.web._http import HttpClient

            return HttpClient()

        return self._resolve("http", _build)


_services_var: ContextVar[Optional[ToolServices]] = ContextVar("tool_services", default=None)

# Lazily-built process-wide default. Never construct at import time: simply
# importing coderAI must not create stores or directories.
_process_default: Optional[ToolServices] = None
_process_default_lock = threading.Lock()


def _default_services() -> ToolServices:
    global _process_default
    if _process_default is None:
        with _process_default_lock:
            if _process_default is None:
                _process_default = ToolServices()
    return _process_default


def get_services() -> ToolServices:
    """Return the active ``ToolServices`` (scoped, else process-wide)."""
    services = _services_var.get()
    return services if services is not None else _default_services()


@contextmanager
def services_scope(
    services: Optional[ToolServices] = None,
    *,
    inherit: bool = False,
    **overrides: Any,
) -> Iterator[ToolServices]:
    """Temporarily bind a ``ToolServices`` container for the current context.

    With no arguments the scope is fully isolated (each service lazily built
    fresh inside the scope). ``inherit=True`` chains to the currently active
    container so only the ``overrides`` differ — used to bind a per-agent
    config while keeping the shared stores.
    """
    if services is None:
        parent = get_services() if inherit else None
        services = ToolServices(parent=parent, **overrides)
    token = _services_var.set(services)
    try:
        yield services
    finally:
        _services_var.reset(token)
