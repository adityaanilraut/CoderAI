from __future__ import annotations

import atexit

from coderai.background.models import Job
from coderai.background.store import JobStore

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


atexit.register(lambda: get_job_store().kill_all(reason="Process shutdown"))
