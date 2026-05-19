"""In-process controller for the Textual chat UI.

``UIBridge`` (despite the legacy name) is no longer an inter-process
boundary — it lives inside the Textual app and translates between the
``event_emitter`` / ``agent_tracker`` world and the timeline state held
by ``coderAI/tui/listeners.py``.
"""

from coderAI.bridge.controller import UIBridge  # noqa: F401
