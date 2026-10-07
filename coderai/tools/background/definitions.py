"""Built-in tool definitions owned by this subsystem."""

from __future__ import annotations
from typing import Any
from coderai.tools.legacy.policy import builtin_effect_policy
from coderai.tools import background as _jobs
from coderai.tools.legacy.schema import define_tool


def register_tools(registry: Any) -> None:
    registry.register(
        define_tool(
            effects=builtin_effect_policy("job_list", False),
            name="job_list",
            description="List your background jobs (running and finished) with their ids, kinds, and statuses.",
            parameters={},
            required=[],
            handler=_jobs.handle_job_list_tool,
            category="meta",
            is_mutating=False,
            is_concurrency_safe=True,
        )
    )

    registry.register(
        define_tool(
            effects=builtin_effect_policy("job_output", False),
            name="job_output",
            description=(
                "Read a background job. Returns output since the previous read. "
                "Every response ends with `[status: ...]`. Set wait=true to block until settlement."
            ),
            parameters={
                "job_id": {
                    "type": "string",
                    "description": "Job id returned when the background work started.",
                },
                "task_id": {
                    "type": "string",
                    "description": "Alias of job_id.",
                },
                "wait": {
                    "type": "boolean",
                    "description": "Block until the job reaches a terminal status or the timeout expires.",
                },
                "block": {
                    "type": "boolean",
                    "description": "Alias of wait.",
                },
                "timeout_ms": {
                    "type": "number",
                    "description": "Max wait in milliseconds when wait is true (default 30000, cap 600000).",
                },
                "timeout": {
                    "type": "number",
                    "description": "Max wait in seconds (converted to ms).",
                },
            },
            required=[],
            handler=_jobs.handle_job_output_tool,
            category="meta",
            is_mutating=False,
            is_concurrency_safe=True,
        )
    )

    registry.register(
        define_tool(
            effects=builtin_effect_policy("job_kill", True),
            name="job_kill",
            description="Request cancellation of a running background job by job id.",
            parameters={
                "job_id": {
                    "type": "string",
                    "description": "Job id returned when the background work started.",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional short reason recorded with the job.",
                },
            },
            required=["job_id"],
            handler=_jobs.handle_job_kill_tool,
            category="meta",
            is_mutating=True,
            is_concurrency_safe=False,
        )
    )
