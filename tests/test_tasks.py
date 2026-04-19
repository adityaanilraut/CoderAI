"""Tests for ManageTasksTool."""

import asyncio
import pytest

from coderAI.tools.tasks import ManageTasksTool


@pytest.fixture
def tool(tmp_path):
    """Return a ManageTasksTool that writes tasks into a temp directory."""
    return ManageTasksTool(), str(tmp_path)


class TestManageTasksTool:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        self.tool = ManageTasksTool()
        self.root = str(tmp_path)

    def _run(self, **kwargs):
        kwargs.setdefault("project_root", self.root)
        return asyncio.run(self.tool.execute(**kwargs))

    # ── list ──────────────────────────────────────────────────────────────────

    def test_list_empty(self):
        result = self._run(action="list")
        assert result["success"]
        # Empty list returns {"tasks": []} without a "total" key
        assert result.get("total", 0) == 0

    # ── add ───────────────────────────────────────────────────────────────────

    def test_add_returns_success(self):
        result = self._run(action="add", title="First task")
        assert result["success"]
        assert result["task"]["id"] == 1
        assert result["task"]["status"] == "pending"

    def test_add_without_title_fails(self):
        result = self._run(action="add")
        assert not result["success"]

    def test_add_default_priority_is_medium(self):
        result = self._run(action="add", title="Task")
        assert result["task"]["priority"] == "medium"

    def test_add_high_priority(self):
        result = self._run(action="add", title="Urgent", priority="high")
        assert result["task"]["priority"] == "high"

    def test_add_invalid_priority_falls_back_to_medium(self):
        result = self._run(action="add", title="Task", priority="critical")
        assert result["task"]["priority"] == "medium"

    def test_add_increments_id(self):
        self._run(action="add", title="T1")
        result = self._run(action="add", title="T2")
        assert result["task"]["id"] == 2

    def test_add_with_description(self):
        result = self._run(action="add", title="Task", description="Some details")
        assert result["task"]["description"] == "Some details"

    # ── start ─────────────────────────────────────────────────────────────────

    def test_start_task(self):
        self._run(action="add", title="Task")
        result = self._run(action="start", task_id=1)
        assert result["success"]
        listed = self._run(action="list")
        assert any(t["status"] == "in_progress" for t in listed["in_progress"])

    def test_start_missing_task_id_fails(self):
        result = self._run(action="start")
        assert not result["success"]

    def test_start_nonexistent_task_fails(self):
        result = self._run(action="start", task_id=999)
        assert not result["success"]

    # ── complete ──────────────────────────────────────────────────────────────

    def test_complete_task(self):
        self._run(action="add", title="Task")
        result = self._run(action="complete", task_id=1)
        assert result["success"]
        listed = self._run(action="list")
        assert any(t["status"] == "completed" for t in listed["completed"])

    def test_complete_sets_completed_at(self):
        self._run(action="add", title="Task")
        self._run(action="complete", task_id=1)
        listed = self._run(action="list")
        task = listed["completed"][0]
        assert task["completed_at"] is not None

    # ── update ────────────────────────────────────────────────────────────────

    def test_update_title(self):
        self._run(action="add", title="Old title")
        result = self._run(action="update", task_id=1, title="New title")
        assert result["success"]
        assert result["task"]["title"] == "New title"

    def test_update_priority(self):
        self._run(action="add", title="Task")
        self._run(action="update", task_id=1, priority="low")
        listed = self._run(action="list")
        task = next(t for t in listed["pending"] if t["id"] == 1)
        assert task["priority"] == "low"

    # ── delete ────────────────────────────────────────────────────────────────

    def test_delete_task(self):
        self._run(action="add", title="Delete me")
        result = self._run(action="delete", task_id=1)
        assert result["success"]
        listed = self._run(action="list")
        assert listed.get("total", 0) == 0

    def test_delete_nonexistent_fails(self):
        result = self._run(action="delete", task_id=42)
        assert not result["success"]

    # ── clear ─────────────────────────────────────────────────────────────────

    def test_clear_removes_completed(self):
        self._run(action="add", title="T1")
        self._run(action="add", title="T2")
        self._run(action="complete", task_id=1)
        result = self._run(action="clear")
        assert result["success"]
        listed = self._run(action="list")
        assert listed["total"] == 1

    def test_clear_keeps_pending(self):
        self._run(action="add", title="Keep me")
        self._run(action="clear")
        listed = self._run(action="list")
        assert listed["total"] == 1

    # ── list grouping ─────────────────────────────────────────────────────────

    def test_list_groups_by_status(self):
        self._run(action="add", title="Pending task")
        self._run(action="add", title="Active task")
        self._run(action="start", task_id=2)
        result = self._run(action="list")
        assert len(result["pending"]) == 1
        assert len(result["in_progress"]) == 1

    # ── unknown action ────────────────────────────────────────────────────────

    def test_unknown_action_fails(self):
        result = self._run(action="fly")
        assert not result["success"]
        assert "Unknown action" in result["error"]
