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
    assert "Build the plan." in SYSTEM_PROMPT_TAIL
    assert "Maintain a task checklist while implementing." in SYSTEM_PROMPT_TAIL


def test_compose_default_system_prompt_includes_directives() -> None:
    registry = ToolRegistry()
    registry.register(CreatePlanTool())
    registry.register(ManageTasksTool())

    rendered = compose_default_system_prompt(registry)

    assert "Plan before you build." in rendered
    assert "### Plan-First Workflow" in rendered
    assert "**Call this first**" in rendered
    assert "Use this alongside `plan`" in rendered
