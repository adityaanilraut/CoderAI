"""Tests for the composed system prompt — verifies plan-first directives ship."""

from __future__ import annotations


from coderAI.system_prompt import (
    SYSTEM_PROMPT_INTRO,
    SYSTEM_PROMPT_RUNTIME,
    SYSTEM_PROMPT_TAIL,
    compose_default_system_prompt,
)
from coderAI.tools.base import ToolRegistry
from coderAI.tools.planning import CreatePlanTool
from coderAI.tools.tasks import ManageTasksTool


def _normalize(text: str) -> str:
    return text.lower()


def test_intro_carries_plan_first_principle() -> None:
    assert "plan before you build" in _normalize(SYSTEM_PROMPT_INTRO)


def test_tail_carries_plan_first_workflow_section() -> None:
    tail = _normalize(SYSTEM_PROMPT_TAIL)
    assert "plan-first" in tail
    # Verify that plan and task management actions are documented
    assert "create_plan" in tail or "action=" in tail
    assert "manage_tasks" in tail


def test_tail_carries_desktop_automation_section() -> None:
    tail = _normalize(SYSTEM_PROMPT_TAIL)
    assert "desktop automation" in tail
    assert "chrome" in tail
    assert "run_applescript" in tail


def test_runtime_carries_execution_loop_section() -> None:
    runtime = _normalize(SYSTEM_PROMPT_RUNTIME)
    assert "execution loop" in runtime
    assert "max_iterations" in runtime
    assert "delegate_task" in runtime


def test_compose_default_system_prompt_includes_directives() -> None:
    registry = ToolRegistry()
    registry.register(CreatePlanTool())
    registry.register(ManageTasksTool())

    rendered = _normalize(compose_default_system_prompt(registry))

    assert "plan before you build" in rendered
    assert "plan-first" in rendered
    assert "execution loop" in rendered
    assert "finish_reason=length" in rendered.lower()
    assert "create_plan" in rendered or "action=" in rendered
    assert "do not duplicate" in rendered.lower()


def test_system_prompt_loaded_from_files() -> None:
    from coderAI.system_prompt import (
        SYSTEM_PROMPT_INTRO,
        SYSTEM_PROMPT_INTERACTION,
        SYSTEM_PROMPT_OUTPUT_STYLE,
        SYSTEM_PROMPT_RUNTIME,
        SYSTEM_PROMPT_TAIL,
    )
    assert len(SYSTEM_PROMPT_INTRO) > 0
    assert len(SYSTEM_PROMPT_RUNTIME) > 0
    assert len(SYSTEM_PROMPT_INTERACTION) > 0
    assert len(SYSTEM_PROMPT_OUTPUT_STYLE) > 0
    assert len(SYSTEM_PROMPT_TAIL) > 0
    assert "You are CoderAI" in SYSTEM_PROMPT_INTRO
    assert "Untrusted Content" in SYSTEM_PROMPT_INTERACTION
    assert "Output & Communication Style" in SYSTEM_PROMPT_OUTPUT_STYLE
    assert "Strategy for Common Tasks" in SYSTEM_PROMPT_TAIL

