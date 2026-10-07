"""Built-in tool definitions owned by this subsystem."""

from __future__ import annotations
from typing import Any
from coderai.tools.legacy.policy import builtin_effect_policy
from coderai.tools import agent as _agents
from coderai.tools import agent as _subagent
from coderai.tools.legacy.schema import define_tool
from coderai.subagents.registry import format_subagent_types_description


def register_tools(registry: Any) -> None:
    registry.register(
        define_tool(
            effects=builtin_effect_policy("Task", False),
            name="Task",
            description="Spawn an isolated sub-agent session for complex, modular, or exploratory tasks. Give it a complete, standalone prompt: it does not share this conversation's context.",
            parameters={
                "description": {
                    "type": "string",
                    "description": "Short 3-5 word summary of the task.",
                },
                "prompt": {
                    "type": "string",
                    "description": "The complete, self-contained task for the subagent. It does not share this conversation's context, so include everything it needs.",
                },
                "subagent_type": {
                    "type": "string",
                    "description": format_subagent_types_description(),
                },
                "mode": {
                    "type": "string",
                    "enum": ["read_only", "general"],
                    "description": "Subagent execution mode ('read_only' or 'general'). Read-only mode disallows mutating tools.",
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": "Whether to run as a background job and return its job id (collect with job_output, stop with job_kill). Defaults to false.",
                },
            },
            required=["description", "prompt"],
            handler=_subagent.handle_subagent_tool,
            category="subagent",
            is_mutating=False,
            is_concurrency_safe=False,
        )
    )

    registry.register(
        define_tool(
            effects=builtin_effect_policy("subagent", False),
            name="subagent",
            description="Start a continuable sub-agent in the background and return an agent id. Use send_message / list_agents / interrupt_agent to steer it. For a one-shot child, use Task or subagent_fork.",
            parameters={
                "description": {
                    "type": "string",
                    "description": "Short 3-5 word summary of the sub-task.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Detailed instructions for the sub-agent.",
                },
                "subagent_type": {
                    "type": "string",
                    "description": format_subagent_types_description(),
                },
                "mode": {
                    "type": "string",
                    "enum": ["read_only", "general"],
                    "description": "Subagent execution mode ('read_only' or 'general'). Read-only mode disallows mutating tools.",
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": "Whether to run in the background and return a durable subagent id immediately. Defaults to true. Set false to wait for the result when your next action depends on it.",
                },
            },
            required=["description", "prompt"],
            handler=_agents.handle_continuable_subagent_tool,
            category="subagent",
            is_mutating=False,
            is_concurrency_safe=False,
        )
    )

    registry.register(
        define_tool(
            effects=builtin_effect_policy("subagent_fork", False),
            name="subagent_fork",
            description="Spawn a one-shot sub-agent and wait for its aggregated findings (alias of Task).",
            parameters={
                "description": {
                    "type": "string",
                    "description": "Short 3-5 word summary of the sub-task.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Detailed instructions for the sub-agent.",
                },
                "subagent_type": {
                    "type": "string",
                    "description": "Builtin agent flavor: coder, explore, or plan.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["read_only", "general"],
                    "description": "Subagent execution mode ('read_only' or 'general'). Read-only mode disallows mutating tools.",
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": "Whether to run as a background job and return its job id (collect with job_output, stop with job_kill). Defaults to false.",
                },
            },
            required=["description", "prompt"],
            handler=_agents.handle_subagent_fork_tool,
            category="subagent",
            is_mutating=False,
            is_concurrency_safe=False,
        )
    )

    registry.register(
        define_tool(
            effects=builtin_effect_policy("send_message", True),
            name="send_message",
            description="Send a follow-up message to a background sub-agent by its subagent id. It becomes the subagent's next turn; if it is still working, the message waits until its current turn finishes.",
            parameters={
                "subagent_id": {
                    "type": "string",
                    "description": "The subagent id returned when the background sub-agent was started (alias: agent_id).",
                },
                "agent_id": {
                    "type": "string",
                    "description": "Alias for subagent_id.",
                },
                "message": {
                    "type": "string",
                    "description": "Follow-up message or instructions.",
                },
            },
            required=["message"],
            handler=_agents.handle_send_message_tool,
            category="subagent",
            is_mutating=True,
            is_concurrency_safe=False,
        )
    )

    registry.register(
        define_tool(
            effects=builtin_effect_policy("interrupt_agent", True),
            name="interrupt_agent",
            description="Cancel a running sub-agent by agent id.",
            parameters={
                "agent_id": {
                    "type": "string",
                    "description": "Target agent id to cancel.",
                }
            },
            required=["agent_id"],
            handler=_agents.handle_interrupt_agent_tool,
            category="subagent",
            is_mutating=True,
            is_concurrency_safe=False,
        )
    )

    registry.register(
        define_tool(
            effects=builtin_effect_policy("list_agents", False),
            name="list_agents",
            description="List sub-agents spawned from this session with ids and statuses.",
            parameters={},
            required=[],
            handler=_agents.handle_list_agents_tool,
            category="subagent",
            is_mutating=False,
            is_concurrency_safe=True,
        )
    )

    registry.register(
        define_tool(
            effects=builtin_effect_policy("report", False),
            name="report",
            description="Child-only: submit the final report for the parent agent and finish this sub-agent.",
            parameters={
                "summary": {
                    "type": "string",
                    "description": "Final report summary for parent.",
                },
                "delivery": {
                    "type": "string",
                    "enum": ["next-step", "quiet"],
                    "description": "Parent scheduling strategy: 'next-step' (default) stages context for parent's next step; 'quiet' appends context silently without waking.",
                },
            },
            required=["summary"],
            handler=_agents.handle_report_tool,
            category="subagent",
            is_mutating=False,
            is_concurrency_safe=False,
        )
    )
