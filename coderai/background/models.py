from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

JobStatus = Literal["running", "stopping", "completed", "killed", "failed"]
DEFAULT_WAIT_TIMEOUT_MS = 30_000
MAX_WAIT_TIMEOUT_MS = 600_000
_MAX_JOBS_PER_SESSION = 100
# Default per-session cap on concurrent running background jobs (configurable via
# CODERAI_MAX_RUNNING_JOBS_PER_SESSION or settings orchestration.maxRunningJobs).
# Static mirror of core.orchestration.DEFAULT_MAX_RUNNING_JOBS (50). Kept as a
# literal so this module stays stdlib-only: importing orchestration pulls in
# the package __init__, which can cycle back here when
# background.* is imported first in a fresh interpreter. The live limit is
# resolved at call time via resolve_max_running_jobs(); this is just the default.
MAX_RUNNING_JOBS_PER_SESSION = 50


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


# --- CoderAI parity (coderai/background/models.py) ---
# Task-kind vocabulary shared by background/ids.py + background/summary.py.
# The full TaskSpec/TaskView/BackgroundTaskStore models arrive with the
# background worker port (Phase 1); these leaves only need the kind literal
# and the terminal-status predicate, so just those live here for now.

TaskKind = Literal["bash", "agent"]
TaskStatus = Literal[
    "created",
    "starting",
    "running",
    "awaiting_approval",
    "completed",
    "failed",
    "killed",
    "lost",
]

TERMINAL_TASK_STATUSES: tuple[TaskStatus, ...] = ("completed", "failed", "killed", "lost")


def is_terminal_status(status: TaskStatus) -> bool:
    return status in TERMINAL_TASK_STATUSES
