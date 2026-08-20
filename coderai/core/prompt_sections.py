"""Ordered system-prompt sections with a stable toolOrder (dsh ctx.systemPrompt)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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
]


@dataclass(frozen=True)
class PromptSection:
    name: str
    order: int
    text: str


def assemble_sections(sections: list[PromptSection]) -> str:
    ordered = sorted(sections, key=lambda s: (s.order, s.name))
    return "\n\n".join(s.text.strip() for s in ordered if s.text and s.text.strip())


def order_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rank = {name: i for i, name in enumerate(TOOL_ORDER)}

    def key(tool: dict[str, Any]) -> tuple[int, str]:
        name = str((tool.get("function") or {}).get("name") or "")
        return (rank.get(name, len(TOOL_ORDER)), name)

    return sorted(tools, key=key)
