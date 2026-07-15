"""Tests for the composed system prompt — verifies task-tracking directives ship."""

from __future__ import annotations


from coderAI.system_prompt import (
    SYSTEM_PROMPT_BROWSER,
    SYSTEM_PROMPT_DESKTOP,
    SYSTEM_PROMPT_INTRO,
    SYSTEM_PROMPT_RUNTIME,
    SYSTEM_PROMPT_TAIL,
    compose_default_system_prompt,
)
from coderAI.tools.base import Tool, ToolRegistry
from coderAI.tools.tasks import ManageTasksTool


def _normalize(text: str) -> str:
    return text.lower()


def test_intro_carries_task_tracking_principle() -> None:
    assert "track multi-step work" in _normalize(SYSTEM_PROMPT_INTRO)
    assert "manage_tasks" in _normalize(SYSTEM_PROMPT_INTRO)


def test_tail_carries_task_workflow_section() -> None:
    tail = _normalize(SYSTEM_PROMPT_TAIL)
    assert "task workflow" in tail
    assert "manage_tasks" in tail
    assert "action=" in tail


class _NamedTool(Tool):
    description = "test capability"
    is_read_only = True

    def __init__(self, name: str) -> None:
        self.name = name

    async def execute(self, **kwargs):
        return {"success": True}


def test_optional_capability_guidance_follows_registered_tools() -> None:
    empty = _normalize(compose_default_system_prompt(ToolRegistry()))
    assert "desktop automation" not in empty
    assert "browser automation" not in empty

    partial_desktop = ToolRegistry()
    partial_desktop.register(_NamedTool("run_applescript"))
    assert "automate and control macos applications" not in _normalize(
        compose_default_system_prompt(partial_desktop)
    )

    desktop = ToolRegistry()
    for name in (
        "run_applescript",
        "get_accessibility_tree",
        "click_ui_element",
        "type_keystrokes",
    ):
        desktop.register(_NamedTool(name))
    desktop_prompt = _normalize(compose_default_system_prompt(desktop))
    assert "automate and control macos applications" in desktop_prompt
    assert "follow this sequence" not in desktop_prompt

    browser = ToolRegistry()
    for name in (
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_select_option",
        "browser_get_content",
        "browser_screenshot",
        "browser_evaluate",
        "browser_wait",
        "browser_close",
    ):
        browser.register(_NamedTool(name))
    browser_prompt = _normalize(compose_default_system_prompt(browser))
    assert "follow this sequence" in browser_prompt
    assert "automate and control macos applications" not in browser_prompt


def test_runtime_carries_execution_loop_section() -> None:
    runtime = _normalize(SYSTEM_PROMPT_RUNTIME)
    assert "execution loop" in runtime
    assert "max_iterations" in runtime
    assert "delegate_task" in runtime


def test_compose_default_system_prompt_includes_directives() -> None:
    registry = ToolRegistry()
    registry.register(ManageTasksTool())

    rendered = _normalize(compose_default_system_prompt(registry))

    assert "track multi-step work" in rendered
    assert "task workflow" in rendered
    assert "execution loop" in rendered
    assert "finish_reason=length" in rendered.lower()
    assert "manage_tasks" in rendered


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
    assert len(SYSTEM_PROMPT_DESKTOP) > 0
    assert len(SYSTEM_PROMPT_BROWSER) > 0
    assert "You are CoderAI" in SYSTEM_PROMPT_INTRO
    assert "Untrusted Content" in SYSTEM_PROMPT_INTERACTION
    assert "Output & Communication Style" in SYSTEM_PROMPT_OUTPUT_STYLE
    assert "Strategy for Common Tasks" in SYSTEM_PROMPT_TAIL
