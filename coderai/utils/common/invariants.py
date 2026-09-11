"""Package-owned runtime invariants and session state integrity verification."""

from __future__ import annotations

from typing import Any


class InvariantViolation(Exception):
    """Raised when a runtime event-stream or session invariant is violated."""

    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        super().__init__(
            f"Runtime invariant violations ({len(violations)}): " + "; ".join(violations)
        )


def verify_monotonic_sequence_numbers(events: list[Any]) -> list[str]:
    """Verify that event sequence numbers are strictly positive and monotonically increasing."""
    violations: list[str] = []
    last_seq: int | None = None

    for idx, ev in enumerate(events):
        seq = (
            getattr(ev, "seq", None)
            if hasattr(ev, "seq")
            else (ev.get("seq") if isinstance(ev, dict) else None)
        )
        if seq is None:
            continue
        if not isinstance(seq, int) or seq <= 0:
            violations.append(
                f"Event at index {idx} has invalid non-positive sequence number: {seq}"
            )
            continue
        if last_seq is not None:
            if seq <= last_seq:
                violations.append(
                    f"Event at index {idx} has non-monotonic sequence number: {seq} <= {last_seq}"
                )
        last_seq = seq

    return violations


def verify_paired_tool_calls(items: list[Any]) -> list[str]:
    """Verify that every tool_call_id emitted by an assistant message has a corresponding tool result."""
    violations: list[str] = []
    pending_tool_calls: dict[str, int] = {}  # tool_call_id -> index

    for idx, item in enumerate(items):
        role = (
            getattr(item, "role", None)
            if hasattr(item, "role")
            else (item.get("role") if isinstance(item, dict) else None)
        )
        ev_type = (
            getattr(item, "type", None)
            if hasattr(item, "type")
            else (item.get("type") if isinstance(item, dict) else None)
        )

        if role == "assistant" or ev_type == "tool/call":
            # Extract tool calls
            tool_calls = (
                getattr(item, "tool_calls", None)
                if hasattr(item, "tool_calls")
                else (item.get("tool_calls") if isinstance(item, dict) else None)
            )
            if not tool_calls and isinstance(item, dict) and "function" in item and "id" in item:
                tool_calls = [item]
            if tool_calls and isinstance(tool_calls, list):
                for tc in tool_calls:
                    tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                    if tc_id:
                        pending_tool_calls[str(tc_id)] = idx

        elif role == "tool" or ev_type == "tool/result":
            tc_id = (
                getattr(item, "tool_call_id", None)
                if hasattr(item, "tool_call_id")
                else (
                    item.get("tool_call_id")
                    if isinstance(item, dict)
                    else (item.get("toolCallId") if isinstance(item, dict) else None)
                )
            )
            if tc_id and str(tc_id) in pending_tool_calls:
                del pending_tool_calls[str(tc_id)]

        elif role == "user" or ev_type == "turn/start":
            # Starting a new user turn with unfulfilled tool calls is a violation
            if pending_tool_calls:
                for unfulfilled_id, declared_idx in list(pending_tool_calls.items()):
                    violations.append(
                        f"Unfulfilled tool_call_id '{unfulfilled_id}' declared at index {declared_idx} before next user/turn boundary at index {idx}"
                    )
                pending_tool_calls.clear()

    # Final check at end of stream
    if pending_tool_calls:
        for unfulfilled_id, declared_idx in pending_tool_calls.items():
            violations.append(
                f"Dangling tool_call_id '{unfulfilled_id}' declared at index {declared_idx} without matching tool result"
            )

    return violations


def verify_turn_step_boundaries(events: list[Any]) -> list[str]:
    """Verify that turn and step lifecycle events are strictly paired."""
    violations: list[str] = []
    in_turn = False
    in_step = False

    for idx, ev in enumerate(events):
        ev_type = (
            getattr(ev, "type", None)
            if hasattr(ev, "type")
            else (ev.get("type") if isinstance(ev, dict) else None)
        )
        if not ev_type:
            continue

        if ev_type == "turn/start":
            if in_turn:
                violations.append(
                    f"Nested turn/start at index {idx} while previous turn is still open"
                )
            in_turn = True
        elif ev_type == "turn/end":
            if not in_turn:
                violations.append(f"Orphan turn/end at index {idx} without open turn")
            if in_step:
                violations.append(f"turn/end at index {idx} while step is still open")
                in_step = False
            in_turn = False
        elif ev_type == "step/start":
            if not in_turn:
                violations.append(f"step/start at index {idx} outside of an active turn")
            if in_step:
                violations.append(
                    f"Nested step/start at index {idx} while previous step is still open"
                )
            in_step = True
        elif ev_type == "step/end":
            if not in_step:
                violations.append(f"Orphan step/end at index {idx} without open step")
            in_step = False

    return violations


def verify_session_invariants(events_or_messages: list[Any]) -> list[str]:
    """Perform comprehensive invariant checks over a list of session events or messages."""
    violations: list[str] = []
    if not events_or_messages:
        return violations

    violations.extend(verify_monotonic_sequence_numbers(events_or_messages))
    violations.extend(verify_paired_tool_calls(events_or_messages))
    violations.extend(verify_turn_step_boundaries(events_or_messages))
    return violations


def assert_session_invariants(events_or_messages: list[Any]) -> None:
    """Assert all session invariants hold, raising InvariantViolation if broken."""
    violations = verify_session_invariants(events_or_messages)
    if violations:
        raise InvariantViolation(violations)
