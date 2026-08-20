"""job_list / job_output / job_kill — model-facing background job controls."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from coderai.core.jobs import (
    DEFAULT_WAIT_TIMEOUT_MS,
    MAX_WAIT_TIMEOUT_MS,
    get_job_store,
    status_line,
)
from coderai.core.tools.types import ToolExecutionContext, ToolResult, as_str


def _session_id(context: ToolExecutionContext | Any) -> str:
    return str(getattr(context, "session_id", "") or "")


async def handle_job_list_tool(
    args: dict[str, Any], context: ToolExecutionContext | Any
) -> ToolResult:
    del args
    jobs = get_job_store().list(_session_id(context))
    if not jobs:
        return ToolResult(ok=True, name="job_list", output="(no background jobs)")
    lines = [f"{j.id} [{j.kind}] {j.status} — {j.label}" for j in jobs]
    return ToolResult(
        ok=True,
        name="job_list",
        output="\n".join(lines),
        metadata={"jobs": [j.to_public_dict() for j in jobs]},
    )


async def handle_job_output_tool(
    args: dict[str, Any], context: ToolExecutionContext | Any
) -> ToolResult:
    job_id = as_str(args.get("job_id")).strip()
    if not job_id:
        return ToolResult(ok=False, name="job_output", error="ValidationError: job_id is required.")

    store = get_job_store()
    session_id = _session_id(context)
    wait = bool(args.get("wait"))
    if wait:
        timeout_ms = args.get("timeout_ms")
        try:
            timeout_ms = int(timeout_ms) if timeout_ms is not None else DEFAULT_WAIT_TIMEOUT_MS
        except (TypeError, ValueError):
            timeout_ms = DEFAULT_WAIT_TIMEOUT_MS
        timeout_ms = max(1, min(timeout_ms, MAX_WAIT_TIMEOUT_MS))
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        while time.monotonic() < deadline:
            job = store.get(job_id, session_id)
            if job is None:
                return ToolResult(
                    ok=False,
                    name="job_output",
                    error=f"Unknown job_id: {job_id}",
                )
            if job.status not in ("running", "stopping"):
                break
            await asyncio.sleep(0.05)

    read = store.read_output(job_id, session_id)
    if read is None:
        return ToolResult(ok=False, name="job_output", error=f"Unknown job_id: {job_id}")
    text, job = read
    body = text if text else "(no new output)"
    separator = "" if body.endswith("\n") else "\n"
    return ToolResult(
        ok=True,
        name="job_output",
        output=f"{body}{separator}{status_line(job)}",
        metadata={"job": job.to_public_dict()},
    )


async def handle_job_kill_tool(
    args: dict[str, Any], context: ToolExecutionContext | Any
) -> ToolResult:
    job_id = as_str(args.get("job_id")).strip()
    if not job_id:
        return ToolResult(ok=False, name="job_kill", error="ValidationError: job_id is required.")
    reason = as_str(args.get("reason")).strip() or None
    store = get_job_store()
    session_id = _session_id(context)
    outcome = store.kill(job_id, session_id, reason)
    job = store.get(job_id, session_id)
    if outcome == "not-found" or job is None:
        return ToolResult(ok=False, name="job_kill", error=f"Unknown job_id: {job_id}")
    if outcome == "already-finished":
        return ToolResult(
            ok=True,
            name="job_kill",
            output=f"job {job.id} had already finished {status_line(job)}",
            metadata={"outcome": outcome, "job": job.to_public_dict()},
        )
    return ToolResult(
        ok=True,
        name="job_kill",
        output=f"requested cancellation of job {job.id}",
        metadata={"outcome": outcome, "job": job.to_public_dict()},
    )
