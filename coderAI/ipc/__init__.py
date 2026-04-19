"""IPC bridge for the TypeScript Ink UI.

The Ink UI speaks NDJSON over stdio (see `ui/PROTOCOL.md`). The classes in
this package translate between the existing `event_emitter` + `agent_tracker`
world and that wire protocol so the Ink UI can be developed in isolation.
"""

from .jsonrpc_server import IPCServer  # noqa: F401
