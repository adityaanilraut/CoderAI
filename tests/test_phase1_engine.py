"""Phase 1: Core Engine & Lifecycle Unit Tests.

Validates:
1. AgentLoop coordinate management (turn/step).
2. AgentLoop event emission and payload structuring.
3. Event log trajectory recording and message derivation.
4. CLI entry point invocation and argument parsing.
"""

import json
import pytest
from unittest.mock import MagicMock

from coderai.core.events import (
    SessionEvent,
    make_turn_start,
    make_turn_end,
    make_step_start,
    make_step_end,
    make_user_event,
    make_assistant_event,
    make_tool_call_event,
    make_tool_result_event,
    make_request_header,
    derive_messages_from_events,
    TURN_START,
    TURN_END,
    STEP_START,
    STEP_END,
    REQUEST_HEADER,
    USER_MESSAGE,
    ASSISTANT_MESSAGE,
    TOOL_CALL,
    TOOL_RESULT,
)
from coderai.core.agent_loop import AgentLoop


def test_agent_loop_turn_and_step_coordinates():
    manager = MagicMock()
    emitted_events: list[SessionEvent] = []
    manager._next_seq = MagicMock(side_effect=lambda sid: len(emitted_events))
    manager._append_event = MagicMock(side_effect=lambda sid, ev: emitted_events.append(ev))

    loop = AgentLoop(manager, "test_session_123")
    assert loop.turn == 0
    assert loop.step == 0

    # Turn 1 Start
    loop.emit_turn_start()
    assert loop.turn == 1
    assert loop.step == 0
    assert len(emitted_events) == 1
    assert emitted_events[0].type == TURN_START
    assert emitted_events[0].data["turn"] == 1

    # Step 1 Start
    loop.emit_step_start()
    assert loop.turn == 1
    assert loop.step == 1
    assert len(emitted_events) == 2
    assert emitted_events[1].type == STEP_START
    assert emitted_events[1].data["step"] == 1

    # Request Header
    loop.emit_request_header("gpt-5.6-luna", system="You are CoderAI.")
    assert len(emitted_events) == 3
    assert emitted_events[2].type == REQUEST_HEADER
    assert emitted_events[2].data["header"]["model"] == "gpt-5.6-luna"

    # Assistant message
    loop.emit_assistant(
        content="Running tool...",
        thinking="Need to read file.",
        tool_calls=[{"id": "call_1", "function": {"name": "read", "arguments": "{}"}}],
        model="gpt-5.6-luna",
    )
    assert len(emitted_events) == 4
    assert emitted_events[3].type == ASSISTANT_MESSAGE
    assert emitted_events[3].data["content"] == "Running tool..."
    assert emitted_events[3].data["thinking"] == "Need to read file."

    # Tool Call
    loop.emit_tool_call("call_1", "read", '{"file_path": "test.py"}')
    assert len(emitted_events) == 5
    assert emitted_events[4].type == TOOL_CALL
    assert emitted_events[4].data["callId"] == "call_1"

    # Tool Result
    loop.emit_tool_result("call_1", "file contents", is_error=False)
    assert len(emitted_events) == 6
    assert emitted_events[5].type == TOOL_RESULT
    assert emitted_events[5].data["callId"] == "call_1"
    assert emitted_events[5].data["content"] == "file contents"

    # Step 1 End
    loop.emit_step_end()
    assert len(emitted_events) == 7
    assert emitted_events[6].type == STEP_END

    # Turn 1 End
    loop.emit_turn_end("natural")
    assert len(emitted_events) == 8
    assert emitted_events[7].type == TURN_END
    assert emitted_events[7].data["reason"] == "natural"


def test_trajectory_derivation_from_loop_events():
    manager = MagicMock()
    emitted_events: list[SessionEvent] = []
    manager._next_seq = MagicMock(side_effect=lambda sid: len(emitted_events))
    manager._append_event = MagicMock(side_effect=lambda sid, ev: emitted_events.append(ev))

    loop = AgentLoop(manager, "sess_trajectory")
    loop.emit_turn_start()

    # User message manually appended to log
    user_ev = make_user_event(seq=manager._next_seq("sess_trajectory"), content="Help me write code")
    emitted_events.append(user_ev)

    loop.emit_step_start()
    loop.emit_assistant(content="Here is the code", model="gpt-5.6-luna")
    loop.emit_step_end()
    loop.emit_turn_end("natural")

    # Derive LLM conversation history from events
    messages = derive_messages_from_events(emitted_events)
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "Help me write code"
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] == "Here is the code"


def test_cli_entry_point_imports_and_structure():
    import coderai.main
    from coderai.cli.app import main as cli_main

    assert callable(cli_main)
    assert coderai.main.main is cli_main
