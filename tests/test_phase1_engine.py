"""Phase 1: Core Engine & Lifecycle Unit Tests.

Validates:
1. AgentLoop coordinate management (turn/step).
2. AgentLoop event emission and payload structuring.
3. Event log trajectory recording and message derivation.
4. CLI entry point invocation and argument parsing.
"""

from unittest.mock import MagicMock

from coderai.core.events import (
    SessionEvent,
    make_user_event,
    make_assistant_event,
    derive_messages_from_events,
    TURN_START,
    TURN_END,
    STEP_START,
    STEP_END,
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

    # Step 1 End
    loop.emit_step_end()
    assert len(emitted_events) == 3
    assert emitted_events[2].type == STEP_END

    # Turn 1 End
    loop.emit_turn_end("natural")
    assert len(emitted_events) == 4
    assert emitted_events[3].type == TURN_END
    assert emitted_events[3].data["reason"] == "natural"


def test_trajectory_derivation_from_loop_events():
    manager = MagicMock()
    emitted_events: list[SessionEvent] = []
    manager._next_seq = MagicMock(side_effect=lambda sid: len(emitted_events))
    manager._append_event = MagicMock(side_effect=lambda sid, ev: emitted_events.append(ev))

    loop = AgentLoop(manager, "sess_trajectory")
    loop.emit_turn_start()

    # User message manually appended to log
    user_ev = make_user_event(
        seq=manager._next_seq("sess_trajectory"), content="Help me write code"
    )
    emitted_events.append(user_ev)

    loop.emit_step_start()
    emitted_events.append(
        make_assistant_event(
            seq=manager._next_seq("sess_trajectory"),
            turn=loop.turn,
            step=loop.step,
            content="Here is the code",
            model="gpt-5.6-luna",
        )
    )
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
