"""Background-job tools: run long tools detached from the turn.

``start_job`` submits any ``backgroundable`` tool (run_tests, package_manager,
lint, download_file) to the shared :class:`~coderAI.core.jobs.JobManager` and
returns immediately with a job id; ``job_status`` / ``wait_job`` observe it and
``job_result`` collects the payload. ``start_job`` needs the live agent's tool
registry, so it is constructed with the agent and registered manually in
``Agent._create_tool_registry`` (same pattern as ``ManageContextTool``); the
other four are zero-arg and auto-discovered.
"""

import logging
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, ValidationError

from coderAI.core.provenance import Provenance
from coderAI.core.services import get_services
from coderAI.core.tool_error_codes import ToolErrorCode
from coderAI.tools.base import SUBPROCESS_TIMEOUT_MARGIN_SECONDS, Tool

logger = logging.getLogger(__name__)

# Tools in this module — a job must never target the job machinery itself.
_JOB_TOOL_NAMES = frozenset({"start_job", "job_status", "job_result", "wait_job", "cancel_job"})


def _unknown_job_error(job_id: str) -> Dict[str, Any]:
    return {
        "success": False,
        "error": f"No background job with id '{job_id}'. Use job_status to list jobs.",
        "error_code": ToolErrorCode.TOOL_ERROR,
    }


class StartJobParams(BaseModel):
    tool_name: str = Field(..., description="Backgroundable tool to run (e.g. run_tests)")
    arguments: Dict[str, Any] = Field(
        default_factory=dict, description="Arguments to pass to the target tool"
    )


class StartJobTool(Tool):
    """Submit a backgroundable tool as a detached job."""

    name = "start_job"
    description = (
        "Run a long-running tool as a background job and return a job_id immediately, "
        "so you can keep working while it runs. Only 'backgroundable' tools are allowed "
        "(run_tests, package_manager, lint, download_file). Track the job with "
        "job_status/wait_job and collect output with job_result."
    )
    parameters_model = StartJobParams
    # The single approval on start_job is the approval for the whole job —
    # the target runs unattended, so gate every start per-call, never blanket,
    # and fail closed once the turn has ingested untrusted content.
    requires_confirmation = True
    high_risk_no_blanket = True
    is_egress = True
    category = "agent"

    def __init__(self, agent: Any) -> None:
        super().__init__()
        self._agent = agent

    async def execute(  # type: ignore[override]
        self, tool_name: str, arguments: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        from coderAI.core.tool_executor import resolve_tool_timeout

        arguments = dict(arguments or {})
        registry = getattr(self._agent, "tools", None)
        if registry is None:
            return {
                "success": False,
                "error": "start_job has no live tool registry.",
                "error_code": ToolErrorCode.TOOL_ERROR,
            }

        if tool_name in _JOB_TOOL_NAMES:
            return {
                "success": False,
                "error": f"'{tool_name}' is part of the job machinery and cannot itself be a job.",
                "error_code": ToolErrorCode.TOOL_ERROR,
            }
        if tool_name == "delegate_task":
            return {
                "success": False,
                "error": "delegate_task cannot run as a background job — call it directly.",
                "error_code": ToolErrorCode.TOOL_ERROR,
            }

        target = registry.get(tool_name)
        if target is None:
            return {
                "success": False,
                "error": f"Unknown tool: '{tool_name}'.",
                "error_code": ToolErrorCode.TOOL_ERROR,
            }
        if not getattr(target, "backgroundable", False):
            allowed = sorted(
                name for name, t in registry.tools.items() if getattr(t, "backgroundable", False)
            )
            return {
                "success": False,
                "error": (
                    f"Tool '{tool_name}' is not backgroundable. "
                    f"Backgroundable tools: {', '.join(allowed) or '(none)'}."
                ),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }

        # Validate arguments in the foreground so a typo fails here, not
        # minutes later inside a detached task.
        if target.parameters_model is not None:
            try:
                arguments = target.parameters_model(**arguments).model_dump()
            except ValidationError as e:
                return {
                    "success": False,
                    "error": f"Validation error for tool '{tool_name}':\n{e}",
                    "error_code": ToolErrorCode.VALIDATION,
                }

        timeout = resolve_tool_timeout(target, tool_name, arguments)
        tracker_info = getattr(self._agent, "tracker_info", None)
        owner = getattr(tracker_info, "agent_id", None) if tracker_info else None

        job_id = get_services().jobs.submit(
            tool_name,
            arguments,
            registry=registry,
            timeout=timeout,
            owner_agent_id=owner,
        )
        return {
            "success": True,
            "job_id": job_id,
            "tool_name": tool_name,
            "timeout_seconds": timeout,
            "message": (
                f"Job {job_id} started for '{tool_name}'. "
                "Use job_status/wait_job to track it and job_result to collect output."
            ),
        }


class JobStatusParams(BaseModel):
    job_id: Optional[str] = Field(
        None, description="Job id to inspect; omit to list all tracked jobs"
    )


class JobStatusTool(Tool):
    """Report the status of one or all background jobs."""

    name = "job_status"
    description = (
        "Check the status of a background job started with start_job "
        "(queued/running/done/failed/timeout/cancelled). Omit job_id to list all jobs."
    )
    parameters_model = JobStatusParams
    is_read_only = True
    category = "agent"

    async def execute(self, job_id: Optional[str] = None) -> Dict[str, Any]:  # type: ignore[override]
        jobs = get_services().jobs
        jobs.prune_finished()
        if job_id is not None:
            record = jobs.get(job_id)
            if record is None:
                return _unknown_job_error(job_id)
            return {"success": True, "job": record.summary()}
        records = jobs.list()
        return {
            "success": True,
            "jobs": [r.summary() for r in records],
            "count": len(records),
        }


class JobResultParams(BaseModel):
    job_id: str = Field(..., description="Job id to collect the result for")


class JobResultTool(Tool):
    """Collect the result payload of a finished background job."""

    name = "job_result"
    description = (
        "Get the full result of a finished background job. If the job is still "
        "running, returns its current status instead — use wait_job to block for it."
    )
    parameters_model = JobResultParams
    is_read_only = True
    # A job may have fetched external content (download_file); relaying its
    # payload must taint the turn exactly as the foreground tool would have.
    # Deliberately conservative: applies to every job's result.
    result_provenance = Provenance.UNTRUSTED_EXTERNAL
    category = "agent"

    async def execute(self, job_id: str) -> Dict[str, Any]:  # type: ignore[override]
        record = get_services().jobs.get(job_id)
        if record is None:
            return _unknown_job_error(job_id)
        if not record.is_finished:
            return {
                "success": False,
                "error": f"Job {job_id} is still {record.status.value} — result not ready. "
                "Use wait_job to block until it finishes.",
                "job": record.summary(),
            }
        payload: Dict[str, Any] = {
            "success": True,
            "job": record.summary(),
        }
        if record.result is not None:
            payload["result"] = record.result
        return payload


class WaitJobParams(BaseModel):
    job_id: str = Field(..., description="Job id to wait for")
    timeout: int = Field(
        60, description="Maximum seconds to wait for the job to finish (1-600, default 60)"
    )


class WaitJobTool(Tool):
    """Block (up to a bounded timeout) until a background job finishes."""

    name = "wait_job"
    description = (
        "Wait for a background job to finish, up to `timeout` seconds (default 60, max 600). "
        "Returns the job's status either way; a still-running job is not cancelled."
    )
    parameters_model = WaitJobParams
    is_read_only = True
    category = "agent"

    @staticmethod
    def _clamp(timeout: Any) -> float:
        try:
            requested = int(timeout)
        except (TypeError, ValueError):
            requested = 60
        return float(max(1, min(requested, 600)))

    def resolve_timeout(self, arguments: Dict[str, Any]) -> Optional[float]:
        # The executor's outer cap must sit above the requested wait window.
        return self._clamp(arguments.get("timeout", 60)) + SUBPROCESS_TIMEOUT_MARGIN_SECONDS

    async def execute(self, job_id: str, timeout: int = 60) -> Dict[str, Any]:  # type: ignore[override]
        wait_seconds = self._clamp(timeout)
        record = await get_services().jobs.wait(job_id, timeout=wait_seconds)
        if record is None:
            return _unknown_job_error(job_id)
        return {
            "success": True,
            "finished": record.is_finished,
            "job": record.summary(),
            **(
                {}
                if record.is_finished
                else {"message": f"Job still {record.status.value} after {wait_seconds:.0f}s."}
            ),
        }


class CancelJobParams(BaseModel):
    job_id: str = Field(..., description="Job id to cancel")


class CancelJobTool(Tool):
    """Cancel a queued or running background job."""

    name = "cancel_job"
    description = "Cancel a background job started with start_job. Finished jobs are unaffected."
    parameters_model = CancelJobParams
    # Mutates only agent-owned job state (tears down a task the agent itself
    # started) — no user files or external effects, so no confirmation.
    safe = True
    category = "agent"

    async def execute(self, job_id: str) -> Dict[str, Any]:  # type: ignore[override]
        jobs = get_services().jobs
        record = jobs.get(job_id)
        if record is None:
            return _unknown_job_error(job_id)
        already_finished = record.is_finished
        record = await jobs.cancel(job_id)
        assert record is not None
        return {
            "success": True,
            "job": record.summary(),
            "message": (
                f"Job {job_id} had already finished ({record.status.value})."
                if already_finished
                else f"Job {job_id} cancelled."
            ),
        }
