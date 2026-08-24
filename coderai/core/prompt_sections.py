"""Ordered system-prompt sections with stable tool ordering."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

TOOL_ORDER_REST = "<unlisted-tools>"

CORE_TOOL_PRESET = frozenset(
    {"bash", "str_replace_editor", "edit", "read", "write", "glob", "grep"}
)
SHELL_EDIT_TOOL_PRESET = frozenset({"bash", "str_replace_editor"})

# Preset names are centralized here so schema and prompt projections cannot drift.
TOOL_PRESETS: dict[str, frozenset[str] | None] = {
    "full": None,
    "core": CORE_TOOL_PRESET,
    "shell_edit": SHELL_EDIT_TOOL_PRESET,
}


def normalize_tool_preset(preset: Any) -> str | None:
    if not isinstance(preset, str):
        return None
    return preset if preset in TOOL_PRESETS else None


def get_preset_tools(preset: Any) -> frozenset[str] | None:
    """Resolve a named preset; unknown and full presets expose all tools."""
    canonical = normalize_tool_preset(preset)
    return TOOL_PRESETS.get(canonical) if canonical is not None else None


def is_restricted_tool_preset(preset: Any) -> bool:
    canonical = normalize_tool_preset(preset)
    return canonical is not None and TOOL_PRESETS[canonical] is not None


TOOL_ORDER = [
    "bash",
    "job_list",
    "job_output",
    "job_kill",
    "glob",
    "grep",
    "read",
    "write",
    "edit",
    "skill",
    "Task",
    "subagent",
    "subagent_fork",
    "send_message",
    "interrupt_agent",
    "list_agents",
    "report",
    "AskUserQuestion",
    "UpdatePlan",
    "todo_write",
    "exit_plan_mode",
    "goal",
    "WebSearch",
    "WebFetch",
    "UnderstandImage",
    "str_replace_editor",
    "terminal_open",
    "terminal_send",
    "terminal_read",
    "terminal_signal",
    "terminal_close",
    "terminal_list",
    "lsp",
    "schedule_create",
    "schedule_list",
    "schedule_delete",
    "workflow",
    "ralph",
    "spawn_teammate",
    "team_task_create",
    "team_task_get",
    "team_task_list",
    "team_task_update",
    "wait_agent",
    "code_mode",
    "session_query",
    "pwsh",
    TOOL_ORDER_REST,
]

PERSONA_ORDER = 0
TOOL_READ_ORDER = 100
TOOL_WRITE_ORDER = 101
TOOL_EDIT_ORDER = 102
TOOL_GREP_ORDER = 104
TOOL_BASH_ORDER = 105
TOOL_PWSH_ORDER = 105
TOOL_JOBS_ORDER = 106
SANDBOX_POLICY_ORDER = 110
SKILLS_CATALOG_ORDER = 110
TOOL_WEB_FETCH_ORDER = 111
TOOL_GOAL_ORDER = 114
SUBAGENT_DELEGATION_ORDER = 120


@dataclass(frozen=True)
class PromptSection:
    name: str
    order: int
    text: str
    complete: bool = False


def assemble_sections(sections: list[PromptSection]) -> str:
    complete_sections = [s for s in sections if s.complete]
    if complete_sections:
        return complete_sections[0].text.strip()
    ordered = sorted(sections, key=lambda s: (s.order, s.name))
    return "\n\n".join(s.text.strip() for s in ordered if s.text and s.text.strip())


def order_tools(
    tools: list[dict[str, Any]], custom_order: list[str] | None = None
) -> list[dict[str, Any]]:
    effective_order = custom_order if custom_order is not None else TOOL_ORDER
    if TOOL_ORDER_REST in effective_order:
        listed_names = [n for n in effective_order if n != TOOL_ORDER_REST]
        tool_by_name: dict[str, list[dict[str, Any]]] = {}
        unlisted: list[dict[str, Any]] = []

        for t in tools:
            name = str((t.get("function") or {}).get("name") or t.get("name") or "")
            if name in listed_names:
                tool_by_name.setdefault(name, []).append(t)
            else:
                unlisted.append(t)

        unlisted_sorted = sorted(
            unlisted,
            key=lambda t: str((t.get("function") or {}).get("name") or t.get("name") or ""),
        )

        result: list[dict[str, Any]] = []
        for item in effective_order:
            if item == TOOL_ORDER_REST:
                result.extend(unlisted_sorted)
            elif item in tool_by_name:
                result.extend(tool_by_name[item])
        return result
    else:
        rank = {name: i for i, name in enumerate(effective_order)}

        def key(tool: dict[str, Any]) -> tuple[int, str]:
            name = str((tool.get("function") or {}).get("name") or tool.get("name") or "")
            return (rank.get(name, len(effective_order)), name)

        return sorted(tools, key=key)
