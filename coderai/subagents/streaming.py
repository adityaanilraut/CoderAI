"""Route child activity through a scoped envelope instead of the parent turn."""

from __future__ import annotations

from typing import Any
from pathlib import Path
import threading
import asyncio

from coderai.wire.emitter import WireEmitter
from coderai.wire.types import SubagentEvent


class ChildWireEmitter(WireEmitter):
    """Keep child events isolated while forwarding their live activity.

    Each nested child adds its own envelope, preserving the execution tree
    without rendering child text as the parent's assistant response.
    """

    def __init__(
        self,
        parent: WireEmitter,
        agent_id: str,
        subagent_type: str | None,
        parent_tool_call_id: str | None = None,
        parent_session_id: str | None = None,
        wire_path: Path | None = None,
    ) -> None:
        super().__init__()
        # Child streams belong to the launching loop even without a UI subscriber.
        # Publishing and close then cannot interleave across provider threads.
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = parent._loop
        self.parent = parent
        self.agent_id = agent_id
        self.subagent_type = subagent_type
        self.parent_tool_call_id = parent_tool_call_id
        self.parent_session_id = parent_session_id
        from coderai.wire.file import WireFile

        self.wire_file = WireFile(wire_path) if wire_path is not None else None
        self._record_lock = threading.Lock()

    def _send(self, msg: Any) -> None:
        with self._lock:
            if self._closed:
                return
        super()._send(msg)
        if self.wire_file is not None:
            try:
                with self._record_lock:
                    self.wire_file.append_message_sync(msg)
            except OSError:
                # A trace sink failing must not suppress live child activity.
                pass
        self.parent.send(
            SubagentEvent(
                event=msg,
                agent_id=self.agent_id,
                subagent_type=self.subagent_type,
                parent_tool_call_id=self.parent_tool_call_id,
            )
        )
        from coderai.orchestration import get_orchestration_event_bus

        get_orchestration_event_bus().emit(
            "subagent/activity",
            {
                "id": self.agent_id,
                "parentSessionId": self.parent_session_id,
                "subagentType": self.subagent_type,
                "event": msg,
            },
        )
