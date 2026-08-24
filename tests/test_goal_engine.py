"""Tests for the goal tool and store."""

from __future__ import annotations

from coderai.core.goals import GoalStore, handle_goal_tool


def test_goal_store_lifecycle(tmp_path):
    store = GoalStore(root_dir=tmp_path)

    # 1. Create goal
    g1 = store.create("sess_1", objective="Implement feature X", max_rounds=5)
    assert g1.id is not None
    assert g1.objective == "Implement feature X"
    assert g1.status == "running"
    assert g1.round == 1
    assert g1.max_rounds == 5

    # 2. Advance round
    g1_adv = store.advance_round("sess_1", g1.id)
    assert g1_adv.round == 2
    assert g1_adv.revision == 2

    # 3. Update goal
    g1_up = store.update("sess_1", g1.id, notes="Work in progress")
    assert g1_up.notes == "Work in progress"

    # 4. Complete goal
    g1_done = store.update("sess_1", g1.id, status="completed")
    assert g1_done.status == "completed"

    # Active goal should now be None
    assert store.get_active_goal("sess_1") is None


def test_handle_goal_tool(tmp_path):
    context = type("Ctx", (), {"project_root": str(tmp_path), "session_id": "sess_3"})()

    # Create goal via tool
    res_create = handle_goal_tool(
        {"action": "create", "objective": "Write unit tests", "max_rounds": 10}, context
    )
    assert res_create.ok is True
    assert "Created goal" in res_create.output

    # Status via tool
    res_status = handle_goal_tool({"action": "status"}, context)
    assert res_status.ok is True
    assert "Write unit tests" in res_status.output

    # Complete via tool
    res_done = handle_goal_tool({"action": "complete", "notes": "All tests passing"}, context)
    assert res_done.ok is True
    assert "COMPLETED" in res_done.output
