"""In-process controller for the Textual chat UI.

``IPCServer`` (despite the legacy name) is no longer an inter-process
boundary — it lives inside the Textual app and translates between the
``event_emitter`` / ``agent_tracker`` world and the timeline state held
by ``coderAI/tui/listeners.py``.
"""

from .jsonrpc_server import IPCServer  # noqa: F401
