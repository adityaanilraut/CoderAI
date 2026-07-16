"""Tests for AgentTracker cancellation behavior."""

from coderAI.core.agent_tracker import AgentStatus, AgentTracker


def test_cancel_descends_through_finished_parent_to_live_child() -> None:
    tracker = AgentTracker()
    root = tracker.register(name="root")
    child = tracker.register(name="child", parent_id=root.agent_id)
    grandchild = tracker.register(name="grandchild", parent_id=child.agent_id)

    child.status = AgentStatus.DONE

    assert tracker.cancel(root.agent_id) is True
    assert root.is_cancelled
    assert grandchild.is_cancelled
