"""Background job manager: run whitelisted tools detached from the turn.

``start_job`` submits a ``backgroundable`` tool call here and returns a job id
immediately; the agent keeps working while the job runs, then polls with
``job_status`` / ``wait_job`` and collects the result with ``job_result``.

Deliberate lifecycle choices (mirroring ``AgentTracker`` records and the
``_tracked_bg_processes`` registry in ``tools/terminal.py``):

* jobs SURVIVE turn cancellation — that is their point; they are torn down by
  ``Agent.close()`` (root agent only) and a best-effort atexit hook;
* concurrency is capped by ``config.max_background_jobs`` via a semaphore
  created lazily per event loop (the TUI runs the agent loop on a worker
  thread, so there is no single process-wide loop to bind to at import time);
* finished records are LRU-pruned beyond :data:`MAX_FINISHED_JOBS`.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
import weakref
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from coderAI.core.services import get_services

logger = logging.getLogger(__name__)

# Upper bound on retained finished-job records (LRU by finished_at); running
# and queued jobs are never pruned.
MAX_FINISHED_JOBS = 50

# Grace period a cancelled job task gets to unwind before we stop waiting.
JOB_CANCEL_GRACE_SECONDS = 2.0

DEFAULT_MAX_BACKGROUND_JOBS = 3


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"  # tool coroutine returned (its dict still carries success)
    FAILED = "failed"  # tool coroutine raised
    TIMEOUT = "timeout"  # per-job wall-clock cap expired
    CANCELLED = "cancelled"


_TERMINAL_STATES = frozenset(
    {JobStatus.DONE, JobStatus.FAILED, JobStatus.TIMEOUT, JobStatus.CANCELLED}
)


@dataclass
class JobRecord:
    """One submitted background tool call and its lifecycle state."""

    job_id: str
    tool_name: str
    arguments: Dict[str, Any]
    status: JobStatus = JobStatus.QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    timeout_seconds: Optional[float] = None
    owner_agent_id: Optional[str] = None
    task: Optional["asyncio.Task[None]"] = field(default=None, repr=False)
    loop: Optional[asyncio.AbstractEventLoop] = field(default=None, repr=False)

    @property
    def is_finished(self) -> bool:
        return self.status in _TERMINAL_STATES

    def summary(self) -> Dict[str, Any]:
        """Small status dict for ``job_status`` listings (no result payload)."""
        data: Dict[str, Any] = {
            "job_id": self.job_id,
            "tool_name": self.tool_name,
            "status": self.status.value,
            "elapsed_seconds": round(
                (self.finished_at or time.time()) - (self.started_at or self.created_at), 2
            ),
        }
        if self.error:
            data["error"] = self.error
        return data


class JobManager:
    """Tracks and runs background tool jobs on the submitting event loop."""

    def __init__(self) -> None:
        self._jobs: Dict[str, JobRecord] = {}
        # Semaphore per event loop (WeakKeyDictionary so a dead worker-thread
        # loop doesn't pin its semaphore forever).
        self._semaphores: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = weakref.WeakKeyDictionary()
        self._atexit_registered = False

    # ── configuration ────────────────────────────────────────────────────

    def _max_jobs(self) -> int:
        try:
            cap = int(
                getattr(get_services().config, "max_background_jobs", DEFAULT_MAX_BACKGROUND_JOBS)
            )
            return max(1, min(16, cap))
        except Exception:
            return DEFAULT_MAX_BACKGROUND_JOBS

    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        sem = self._semaphores.get(loop)
        if sem is None:
            sem = asyncio.Semaphore(self._max_jobs())
            self._semaphores[loop] = sem
        return sem

    # ── submission / execution ───────────────────────────────────────────

    def submit(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        *,
        registry: Any,
        timeout: float,
        owner_agent_id: Optional[str] = None,
    ) -> str:
        """Start *tool_name* as a background job; returns the job id.

        Must be called from a running event loop (tool execute context). The
        caller is responsible for gating (confirmation, backgroundable check,
        argument validation) — this is pure lifecycle.
        """
        job_id = f"job_{uuid.uuid4().hex[:8]}"
        record = JobRecord(
            job_id=job_id,
            tool_name=tool_name,
            arguments=dict(arguments),
            timeout_seconds=timeout,
            owner_agent_id=owner_agent_id,
            loop=asyncio.get_running_loop(),
        )
        self._jobs[job_id] = record
        record.task = asyncio.create_task(self._run_job(record, registry))
        self._ensure_atexit_cleanup()
        self.prune_finished()
        return job_id

    async def _run_job(self, record: JobRecord, registry: Any) -> None:
        try:
            async with self._semaphore():
                record.status = JobStatus.RUNNING
                record.started_at = time.time()
                try:
                    result = await asyncio.wait_for(
                        registry.execute(record.tool_name, **record.arguments),
                        timeout=record.timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    record.status = JobStatus.TIMEOUT
                    record.error = (
                        f"Job exceeded its timeout of {record.timeout_seconds:.0f}s"
                        if record.timeout_seconds
                        else "Job timed out"
                    )
                except Exception as e:
                    record.status = JobStatus.FAILED
                    record.error = str(e)
                else:
                    record.result = (
                        result if isinstance(result, dict) else {"success": True, "result": result}
                    )
                    record.status = JobStatus.DONE
        except asyncio.CancelledError:
            record.status = JobStatus.CANCELLED
            record.error = record.error or "Job was cancelled."
            raise
        finally:
            # The task is ending, so whatever the path (including a failure
            # acquiring the semaphore) the record must land in a terminal state.
            if not record.is_finished:
                record.status = JobStatus.FAILED
                record.error = record.error or "Job exited unexpectedly."
            record.finished_at = time.time()
            self._emit_terminal(record)
            self.prune_finished()

    def _emit_terminal(self, record: JobRecord) -> None:
        try:
            get_services().events.emit(
                "agent_status",
                message=(
                    f"[dim]Background job {record.job_id} ({record.tool_name}) "
                    f"finished: {record.status.value}[/dim]"
                ),
            )
        except Exception:
            logger.debug("job terminal-state event emit failed", exc_info=True)

    # ── queries ──────────────────────────────────────────────────────────

    def get(self, job_id: str) -> Optional[JobRecord]:
        return self._jobs.get(job_id)

    def list(self) -> List[JobRecord]:
        return list(self._jobs.values())

    # ── lifecycle ────────────────────────────────────────────────────────

    async def wait(self, job_id: str, timeout: float) -> Optional[JobRecord]:
        """Wait up to *timeout* seconds for the job to finish.

        Returns the record (caller inspects ``status`` — it may still be
        running after the wait), or ``None`` for an unknown id. Never cancels
        or unwinds the job itself.
        """
        record = self._jobs.get(job_id)
        if record is None:
            return None
        if record.task is not None and not record.task.done():
            # asyncio.wait: no cancellation and no exception propagation on
            # timeout — exactly the "observe, don't disturb" semantics needed.
            await asyncio.wait({record.task}, timeout=timeout)
        return record

    async def cancel(self, job_id: str) -> Optional[JobRecord]:
        """Cancel a running/queued job (2s grace), or no-op if finished."""
        record = self._jobs.get(job_id)
        if record is None:
            return None
        if record.is_finished or record.task is None:
            return record
        record.task.cancel()
        try:
            await asyncio.wait_for(record.task, timeout=JOB_CANCEL_GRACE_SECONDS)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        if not record.is_finished:
            record.status = JobStatus.CANCELLED
            record.error = record.error or "Job was cancelled."
            record.finished_at = time.time()
        return record

    def prune_finished(self) -> int:
        """Drop the oldest finished records beyond :data:`MAX_FINISHED_JOBS`."""
        finished = [r for r in self._jobs.values() if r.is_finished]
        excess = len(finished) - MAX_FINISHED_JOBS
        if excess <= 0:
            return 0
        finished.sort(key=lambda r: r.finished_at or 0.0)
        for record in finished[:excess]:
            del self._jobs[record.job_id]
        return excess

    async def shutdown(self) -> None:
        """Cancel every live job and await their teardown (same-loop callers)."""
        live = [r.task for r in self._jobs.values() if r.task is not None and not r.task.done()]
        for task in live:
            task.cancel()
        if live:
            await asyncio.gather(*live, return_exceptions=True)

    def cancel_all_threadsafe(self) -> None:
        """Best-effort cross-thread cancellation for atexit teardown."""
        for record in self._jobs.values():
            task, loop = record.task, record.loop
            if task is not None and not task.done() and loop is not None and not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(task.cancel)
                except RuntimeError:
                    pass  # loop already shutting down

    def _ensure_atexit_cleanup(self) -> None:
        if not self._atexit_registered:
            import atexit

            atexit.register(self.cancel_all_threadsafe)
            self._atexit_registered = True
