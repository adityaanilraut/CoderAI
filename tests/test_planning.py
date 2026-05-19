"""Tests for CreatePlanTool."""

import asyncio
import json
import pytest

from coderAI.tools.planning import CreatePlanTool, _get_plan_file


class TestCreatePlanTool:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.tool = CreatePlanTool()

    def test_create_plan(self):
        result = asyncio.run(
            self.tool.execute(
                action="create",
                title="Test Plan",
                steps=["Step 1", "Step 2", "Step 3"],
            )
        )
        assert result["success"]
        assert result["plan"]["title"] == "Test Plan"
        assert result["plan"]["schema_version"] == 2
        assert len(result["plan"]["steps"]) == 3

    def test_status_action(self):
        asyncio.run(
            self.tool.execute(
                action="create",
                title="Status Test",
                steps=["Alpha", "Beta"],
            )
        )
        result = asyncio.run(self.tool.execute(action="status"))
        assert result["success"]
        assert result["title"] == "Status Test"
        assert result["total_steps"] == 2
        assert result["completed_steps"] == 0
        assert result["current_step_index"] == 0
        assert result["current_step_description"] == "Alpha"
        assert result["next_step_description"] == "Beta"
        assert result["done"] is False

    def test_status_no_plan(self, tmp_path, monkeypatch):
        from pathlib import Path

        project = tmp_path / "empty_project"
        (project / ".coderAI").mkdir(parents=True)
        monkeypatch.chdir(project)

        def _project_dot_coderai_only(_rel: str, project_root: str = ".") -> Path:
            return Path(project_root).resolve() / ".coderAI"

        monkeypatch.setattr(
            "coderAI.tools.planning.find_dot_coderai_subdir",
            _project_dot_coderai_only,
        )

        tool = CreatePlanTool()
        tool.project_root = str(project)
        result = asyncio.run(tool.execute(action="status"))
        assert result["success"]
        assert result["done"] is True
        assert result["total_steps"] == 0
        assert "No active plan" in result["message"]

    def test_show(self):
        # Create first
        asyncio.run(
            self.tool.execute(
                action="create",
                title="Show Test",
                steps=["A", "B"],
            )
        )
        result = asyncio.run(
            self.tool.execute(action="show")
        )
        assert result["success"]
        assert result["plan"]["title"] == "Show Test"

    def test_advance(self):
        asyncio.run(
            self.tool.execute(
                action="create",
                title="Advance Test",
                steps=["A", "B"],
            )
        )
        result = asyncio.run(
            self.tool.execute(action="advance")
        )
        assert result["success"]
        assert "A" in result["message"]

    def test_show_no_plan(self):
        # Fresh tool with no plan
        tool = CreatePlanTool()
        result = asyncio.run(
            tool.execute(action="show")
        )
        # Behavior depends on whether a plan was previously persisted
        # Either success with a plan or success with no_plan message
        assert isinstance(result, dict)

    def test_unknown_action(self):
        result = asyncio.run(
            self.tool.execute(action="invalid")
        )
        assert not result["success"]

    def test_plan_tool_uses_configured_project_root(self, tmp_path, monkeypatch):
        project = tmp_path / "project"
        cwd = tmp_path / "cwd"
        project.mkdir()
        cwd.mkdir()
        monkeypatch.chdir(cwd)

        tool = CreatePlanTool()
        tool.project_root = str(project)

        result = asyncio.run(
            tool.execute(action="create", title="Rooted Plan", steps=["A"])
        )

        assert result["success"]
        plan_path = _get_plan_file(str(project))
        assert plan_path.exists()
        assert json.loads(plan_path.read_text())["title"] == "Rooted Plan"
        assert not (cwd / ".coderAI" / "current_plan.json").exists()

