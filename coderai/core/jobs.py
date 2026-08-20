"""Session-scoped background job registry for bash (and later other kinds)."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Literal

from coderai.core.common.process_tree import kill_process_tree

JobStatus = Literal["running", "stopping", "completed", "killed", "failed"]
DEFAULT_WAIT_TIMEOUT_MS = 30_000
MAX_WAIT_TIMEOUT_MS = 600_000
_MAX_JOBS_PER_SESSION = 100


@dataclass
class Job:
    id: str
    session_id: str
    kind: str
    label: str
    status: JobStatus
    started_at: int
    process_id: int | None = None
    output_path: str | None = None
    finished_at: int | None = None
    detail: str | None = None
    read_offset: int = 0
    exit_code: int | None = None
    signal: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
            "startedAt": self.started_at,
        }
        if self.detail:
            d["detail"] = self.detail
        if self.finished_at is not None:
            d["finishedAt"] = self.finished_at
        return d


class JobStore:
    """Thread-safe in-process job registry keyed by job id, scoped by session."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}

    def start(
        self,
        *,
        job_id: str,
        session_id: str,
        kind: str,
        label: str,
        process_id: int | None = None,
        output_path: str | None = None,
        detail: str | None = None,
    ) -> Job:
        job = Job(
            id=job_id,
            session_id=session_id,
            kind=kind,
            label=label[:240],
            status="running",
            started_at=int(time.time() * 1000),
            process_id=process_id,
            output_path=output_path,
            detail=detail,
        )
        with self._lock:
            self._jobs[job_id] = job
            self._evict_locked(session_id)
        return job

    def complete(
        self,
        job_id: str,
        *,
        ok: bool,
        exit_code: int | None = None,
        signal: str | None = None,
        detail: str | None = None,
    ) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.status in ("completed", "killed", "failed"):
                return job
            if job.status == "stopping":
                job.status = "killed"
                job.detail = job.detail or detail or "killed"
            elif ok:
                job.status = "completed"
                if detail:
                    job.detail = detail
            else:
                job.status = "failed"
                job.detail = (
                    detail or signal or (f"exit {exit_code}" if exit_code is not None else "failed")
                )
            job.finished_at = int(time.time() * 1000)
            job.exit_code = exit_code
            job.signal = signal
            return job

    def kill(self, job_id: str, session_id: str, reason: str | None = None) -> str:
        """Request cancellation. Returns cancellation-requested | already-finished | not-found."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.session_id != session_id:
                return "not-found"
            if job.status in ("completed", "killed", "failed"):
                return "already-finished"
            job.status = "stopping"
            if reason:
                job.detail = reason
            pid = job.process_id
        if pid:
            try:
                kill_process_tree(int(pid))
            except Exception:
                pass
        return "cancellation-requested"

    def get(self, job_id: str, session_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.session_id != session_id:
                return None
            return job

    def list(self, session_id: str) -> list[Job]:
        with self._lock:
            jobs = [j for j in self._jobs.values() if j.session_id == session_id]
        return sorted(jobs, key=lambda j: j.started_at)

    def read_output(self, job_id: str, session_id: str) -> tuple[str, Job] | None:
        job = self.get(job_id, session_id)
        if job is None:
            return None
        text = ""
        if job.output_path:
            try:
                with open(job.output_path, encoding="utf-8", errors="replace") as handle:
                    text = handle.read()
            except OSError:
                text = ""
        with self._lock:
            current = self._jobs.get(job_id)
            if current is None or current.session_id != session_id:
                return None
            new_text = text[current.read_offset :]
            current.read_offset = len(text)
            return new_text, current

    def _evict_locked(self, session_id: str) -> None:
        owned = [j for j in self._jobs.values() if j.session_id == session_id]
        if len(owned) <= _MAX_JOBS_PER_SESSION:
            return
        finished = [j for j in owned if j.status in ("completed", "killed", "failed")]
        finished.sort(key=lambda j: j.finished_at or j.started_at)
        overflow = len(owned) - _MAX_JOBS_PER_SESSION
        for job in finished[:overflow]:
            self._jobs.pop(job.id, None)


_STORE = JobStore()


def get_job_store() -> JobStore:
    return _STORE


def reset_job_store() -> None:
    """Test helper: drop all jobs."""
    with _STORE._lock:
        _STORE._jobs.clear()


def status_line(job: Job) -> str:
    if job.detail:
        return f"[status: {job.status}, {job.detail}]"
    return f"[status: {job.status}]"
