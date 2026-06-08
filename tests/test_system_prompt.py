"""Tests for the composed system prompt — verifies plan-first directives ship."""

from __future__ import annotations

from coderAI.system_prompt import (
    SYSTEM_PROMPT_INTRO,
    SYSTEM_PROMPT_TAIL,
    compose_default_system_prompt,
)
from coderAI.tools.base import ToolRegistry
from coderAI.tools.planning import CreatePlanTool
from coderAI.tools.tasks import ManageTasksTool


def test_intro_carries_plan_first_principle() -> None:
    assert "Plan before you build." in SYSTEM_PROMPT_INTRO


def test_tail_carries_plan_first_workflow_section() -> None:
    assert "### Plan-First Workflow" in SYSTEM_PROMPT_TAIL
    assert "action='create'" in SYSTEM_PROMPT_TAIL
    assert "action='status'" in SYSTEM_PROMPT_TAIL
    assert "manage_tasks" in SYSTEM_PROMPT_TAIL


def test_tail_carries_desktop_automation_section() -> None:
    assert "### macOS Desktop Automation" in SYSTEM_PROMPT_TAIL
    assert "Google Chrome" in SYSTEM_PROMPT_TAIL
    assert "run_applescript" in SYSTEM_PROMPT_TAIL


def test_compose_default_system_prompt_includes_directives() -> None:
    registry = ToolRegistry()
    registry.register(CreatePlanTool())
    registry.register(ManageTasksTool())

    rendered = compose_default_system_prompt(registry)

    assert "Plan before you build." in rendered
    assert "### Plan-First Workflow" in rendered
    assert "finish_reason=length" in rendered
    assert "action='status'" in rendered
    assert "Do not duplicate plan steps into `manage_tasks`" in rendered
