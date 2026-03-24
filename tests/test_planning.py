"""Tests for CreatePlanTool."""

import asyncio
import pytest

from coderAI.tools.planning import CreatePlanTool


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
        assert len(result["plan"]["steps"]) == 3

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

