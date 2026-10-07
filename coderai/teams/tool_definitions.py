"""Built-in tool definitions owned by this subsystem."""

from __future__ import annotations
from typing import Any
from coderai.tools.legacy.policy import builtin_effect_policy
from coderai.teams import (
    handle_spawn_teammate_tool as _spawn_teammate_handle,
    handle_team_task_create_tool as _task_create_handle,
    handle_team_task_get_tool as _task_get_handle,
    handle_team_task_list_tool as _task_list_handle,
    handle_team_task_update_tool as _task_update_handle,
    handle_wait_agent_tool as _wait_agent_handle,
)
from coderai.tools.legacy.schema import define_tool


def register_tools(registry: Any) -> None:
    registry.register(
        define_tool(
            effects=builtin_effect_policy("spawn_teammate", True),
            name="spawn_teammate",
            description="Spawn a specialized teammate agent for concurrent collaboration.",
            parameters={
                "name": {"type": "string", "description": "Teammate identifier/name."},
                "role": {"type": "string", "description": "Role/persona for the teammate."},
                "prompt": {"type": "string", "description": "Task instructions for teammate."},
                "system_prompt": {"type": "string"},
                "mode": {"type": "string", "enum": ["read_only", "general"]},
                "allowed_tools": {"type": "array", "items": {"type": "string"}},
            },
            required=["name", "role", "prompt"],
            handler=_spawn_teammate_handle,
            category="subagent",
            is_mutating=True,
            is_concurrency_safe=False,
        )
    )

    registry.register(
        define_tool(
            effects=builtin_effect_policy("team_task_create", True),
            name="team_task_create",
            description="Create a task on the shared team task board.",
            parameters={
                "title": {"type": "string"},
                "description": {"type": "string"},
                "assigned_to": {"type": "string"},
                "priority": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                "dependencies": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
            },
            required=["title"],
            handler=_task_create_handle,
            category="subagent",
            is_mutating=True,
            is_concurrency_safe=False,
        )
    )

    registry.register(
        define_tool(
            effects=builtin_effect_policy("team_task_get", False),
            name="team_task_get",
            description="Retrieve details of a task from the shared team task board.",
            parameters={"task_id": {"type": "string"}},
            required=["task_id"],
            handler=_task_get_handle,
            category="subagent",
            is_mutating=False,
            is_concurrency_safe=True,
        )
    )

    registry.register(
        define_tool(
            effects=builtin_effect_policy("team_task_list", False),
            name="team_task_list",
            description="List tasks on the shared team task board.",
            parameters={
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed", "blocked", "failed"],
                },
                "assigned_to": {"type": "string"},
            },
            required=[],
            handler=_task_list_handle,
            category="subagent",
            is_mutating=False,
            is_concurrency_safe=True,
        )
    )

    registry.register(
        define_tool(
            effects=builtin_effect_policy("team_task_update", True),
            name="team_task_update",
            description="Update status, assignee, result, or notes for a task on the shared team task board with optimistic CAS locking.",
            parameters={
                "task_id": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed", "blocked", "failed"],
                },
                "assigned_to": {"type": "string"},
                "result": {"type": "string"},
                "notes": {"type": "string"},
                "dependencies": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
                "expected_revision": {
                    "type": "integer",
                    "description": "Expected current task revision for optimistic concurrency check.",
                },
            },
            required=["task_id"],
            handler=_task_update_handle,
            category="subagent",
            is_mutating=True,
            is_concurrency_safe=False,
        )
    )

    registry.register(
        define_tool(
            effects=builtin_effect_policy("wait_agent", False),
            name="wait_agent",
            description="Wait for completion or message settlement from spawned teammates or subagents.",
            parameters={
                "agent_id": {"type": "string"},
                "agent_ids": {"type": "array", "items": {"type": "string"}},
                "timeout_seconds": {"type": "number"},
                "wait_for": {"type": "string", "enum": ["completion", "message", "any_settlement"]},
            },
            required=[],
            handler=_wait_agent_handle,
            category="subagent",
            is_mutating=False,
            is_concurrency_safe=False,
        )
    )
