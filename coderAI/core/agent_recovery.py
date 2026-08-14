"""Transcript repair and recoverable-error feedback."""

import json
import logging
import time as _time
from typing import Any

from coderAI.core.agent_loop_outcomes import RECOVERABLE_ERROR_MARKER
from coderAI.core.ports import AgentRuntime
from coderAI.core.services import get_services
from coderAI.system.error_policy import MAX_CONSECUTIVE_ERRORS
from coderAI.system.history import Message
from coderAI.system.redaction import redact_text

logger = logging.getLogger(__name__)


class RecoveryHandler:
    """Typed mixin contract for transcript recovery."""

    agent: AgentRuntime

    def _repair_unpaired_tool_calls(self) -> None:
        """Ensure assistant tool calls are followed by matching tool results.

        If a previous iteration crashed after writing an assistant message with
        ``tool_calls`` but before tool result messages were appended, some
        providers reject the next request. We synthesize tool-error messages for
        any missing tool IDs so the transcript remains valid and recoverable.

        Uses a two-pass O(n) algorithm: first collect expected and seen IDs,
        then rebuild messages with synthetic injections where needed.
        """
        session = self.agent.session
        if not session or not session.messages:
            return

        msgs = session.messages

        # Pass 1: collect expected tool_call_ids per assistant index and
        # track which tool_call_ids already have corresponding tool messages.
        expected_by_assistant: dict[int, set[str]] = {}
        seen_tool_ids: set[str] = set()
        for i, msg in enumerate(msgs):
            if msg.role == "assistant" and msg.tool_calls:
                ids = set()
                for tc in msg.tool_calls:
                    tc_id = (tc or {}).get("id")
                    if isinstance(tc_id, str) and tc_id:
                        ids.add(tc_id)
                if ids:
                    expected_by_assistant[i] = ids
            elif msg.role == "tool" and msg.tool_call_id:
                seen_tool_ids.add(msg.tool_call_id)

        if not expected_by_assistant:
            return

        # Count total missing tool_call_ids.
        missing_total = sum(
            len(tool_ids - seen_tool_ids) for tool_ids in expected_by_assistant.values()
        )
        if not missing_total:
            return

        # Pass 2: rebuild messages list, injecting synthetic tool responses
        # after each assistant message that has unpaired tool calls.
        repaired: list[Message] = []
        for i, msg in enumerate(msgs):
            repaired.append(msg)
            if i in expected_by_assistant:
                missing = expected_by_assistant[i] - seen_tool_ids
                if missing:
                    anchor_ts = msg.timestamp or _time.time()
                    for offset, tcid in enumerate(sorted(missing), start=1):
                        repaired.append(
                            Message(
                                role="tool",
                                content=json.dumps(
                                    {
                                        "success": False,
                                        "error": (
                                            "Tool execution did not complete due to an internal error. "
                                            "Recovered by adding a synthetic tool response. "
                                            "Synthetic — the tool may have succeeded; verify the filesystem before retrying."
                                        ),
                                        "error_code": "recovered",
                                        "hint": "Synthetic — the tool may have succeeded; verify the filesystem before retrying.",
                                    }
                                ),
                                tool_call_id=tcid,
                                name="internal_recovery",
                                timestamp=anchor_ts + offset * 1e-6,
                            )
                        )

        session.messages = repaired
        session.updated_at = _time.time()
        logger.warning(
            "Recovered %s unpaired assistant tool_call(s) by injecting synthetic tool responses.",
            missing_total,
        )

    async def _handle_recoverable_error(
        self, e: Exception, count: int, user_message: str
    ) -> list[dict[str, Any]]:
        # Sanitize error message to avoid leaking sensitive info (API keys, tracebacks)
        error_str = str(e)
        # Truncate long error messages
        if len(error_str) > 200:
            error_str = error_str[:200] + "..."
        error_str = redact_text(error_str)

        get_services().events.emit(
            "agent_error", message=f"Error (attempt {count}/{MAX_CONSECUTIVE_ERRORS}): {error_str}"
        )
        self._repair_unpaired_tool_calls()

        # Persist the recovery feedback into the session so it survives the
        # next ``messages.clear(); messages.extend(session.get_messages_for_api())``
        # cycle in the tool executor. The ``RECOVERABLE_ERROR_MARKER`` prefix
        # lets the context controller (and downstream sub-agents) recognise
        # and preserve these notes across summarization.
        feedback = (
            f"{RECOVERABLE_ERROR_MARKER} {error_str}. "
            "Do NOT retry the exact same tool call with the same arguments — "
            "that will fail the same way. Either change the arguments, use a "
            "different tool, or explain why you cannot proceed."
        )
        if self.agent.session is not None:
            self.agent.session.add_message("system", feedback)
            messages = self.agent.session.get_messages_for_api()
        else:
            messages = [{"role": "system", "content": feedback}]
        messages = self.agent.context_controller.inject_context(messages, query=user_message)
        return messages
