"""Durable subagent instance records.

Each instance owns a directory under
``<project>/.coderai/sessions/<session>/subagents/<agent_id>/`` with
``meta.json`` (status record), ``prompt.txt``, ``context.jsonl``,
``wire.jsonl``, and ``output``. Statuses: ``idle | running_foreground |
running_background | completed | failed | killed``. Records validate on read;
corrupt files read as ``None`` so one bad instance never breaks listing.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from coderai.utils.io import atomic_json_write
from coderai.log import logger

SubagentStatus = Literal[
    "idle",
    "running_foreground",
    "running_background",
    "completed",
    "failed",
    "killed",
]

VALID_SUBAGENT_STATUSES: tuple[str, ...] = (
    "idle",
    "running_foreground",
    "running_background",
    "completed",
    "failed",
    "killed",
)

TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed", "killed"})


@dataclass(frozen=True, slots=True, kw_only=True)
class SubagentLaunchSpec:
    agent_id: str
    subagent_type: str
    model_override: str | None = None
    effective_model: str | None = None
    created_at: float = field(default_factory=time.time)


@dataclass(frozen=True, slots=True, kw_only=True)
class SubagentInstanceRecord:
    agent_id: str
    subagent_type: str
    status: str
    description: str
    created_at: float
    updated_at: float
    last_task_id: str | None = None
    launch_spec: SubagentLaunchSpec | None = None


def new_agent_id(prefix: str = "agt") -> str:
    """Fresh instance id (``agt_<12 hex>``)."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class SubagentStore:
    """Filesystem-backed instance registry scoped to one session."""

    def __init__(self, session_dir: str | Path) -> None:
        self._session_dir = Path(session_dir)

    # -- paths ----------------------------------------------------------
    @property
    def root(self) -> Path:
        return self._session_dir / "subagents"

    def instance_dir(self, agent_id: str, *, create: bool = False) -> Path:
        path = self.root / str(agent_id)
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def meta_path(self, agent_id: str) -> Path:
        return self.instance_dir(agent_id) / "meta.json"

    def prompt_path(self, agent_id: str) -> Path:
        return self.instance_dir(agent_id) / "prompt.txt"

    def output_path(self, agent_id: str) -> Path:
        return self.instance_dir(agent_id) / "output"

    def context_path(self, agent_id: str) -> Path:
        return self.instance_dir(agent_id) / "context.jsonl"

    # -- writes ---------------------------------------------------------
    def create_instance(
        self,
        *,
        agent_id: str,
        subagent_type: str,
        description: str,
        model_override: str | None = None,
        effective_model: str | None = None,
    ) -> SubagentInstanceRecord:
        """Create the directory skeleton + ``idle`` record."""
        now = time.time()
        self.instance_dir(agent_id, create=True)
        for name in ("context.jsonl", "wire.jsonl", "prompt.txt", "output"):
            try:
                (self.instance_dir(agent_id) / name).touch(exist_ok=True)
            except OSError:
                pass
        record = SubagentInstanceRecord(
            agent_id=agent_id,
            subagent_type=subagent_type,
            status="idle",
            description=description,
            created_at=now,
            updated_at=now,
            launch_spec=SubagentLaunchSpec(
                agent_id=agent_id,
                subagent_type=subagent_type,
                model_override=model_override,
                effective_model=effective_model,
                created_at=now,
            ),
        )
        self.write_instance(record)
        return record

    def write_instance(self, record: SubagentInstanceRecord) -> None:
        self.instance_dir(record.agent_id, create=True)
        atomic_json_write(asdict(record), self.meta_path(record.agent_id))

    def update_instance(
        self,
        agent_id: str,
        *,
        status: str | None = None,
        last_task_id: str | None = None,
    ) -> SubagentInstanceRecord | None:
        """Patch status/task fields (validates status; returns updated record)."""
        record = self.get_instance(agent_id)
        if record is None:
            return None
        if status is not None and status not in VALID_SUBAGENT_STATUSES:
            raise ValueError(f"Invalid subagent status: {status!r}")
        updated = SubagentInstanceRecord(
            agent_id=record.agent_id,
            subagent_type=record.subagent_type,
            status=status or record.status,
            description=record.description,
            created_at=record.created_at,
            updated_at=time.time(),
            last_task_id=last_task_id if last_task_id is not None else record.last_task_id,
            launch_spec=record.launch_spec,
        )
        self.write_instance(updated)
        return updated

    def write_prompt(self, agent_id: str, prompt: str) -> None:
        """Persist the launch prompt (best-effort)."""
        try:
            self.instance_dir(agent_id, create=True)
            self.prompt_path(agent_id).write_text(prompt, encoding="utf-8")
        except OSError as exc:
            logger.debug("Subagent prompt write failed: {error}", error=exc)

    # -- reads ----------------------------------------------------------
    def get_instance(self, agent_id: str) -> SubagentInstanceRecord | None:
        meta = self.meta_path(agent_id)
        if not meta.is_file():
            return None
        try:
            import json

            data = json.loads(meta.read_text(encoding="utf-8"))
            return _record_from_dict(data)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            logger.debug("Subagent record unreadable: {error}", error=exc)
            return None

    def require_instance(self, agent_id: str) -> SubagentInstanceRecord:
        record = self.get_instance(agent_id)
        if record is None:
            raise KeyError(f"Unknown subagent instance: {agent_id}")
        return record

    def list_instances(self) -> list[SubagentInstanceRecord]:
        if not self.root.is_dir():
            return []
        records: list[SubagentInstanceRecord] = []
        for child in sorted(self.root.iterdir()):
            if child.is_dir():
                record = self.get_instance(child.name)
                if record is not None:
                    records.append(record)
        return records

    def delete_instance(self, agent_id: str) -> None:
        import shutil

        target = self.instance_dir(agent_id)
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)

    def mark_stale_foreground_failed(self) -> list[str]:
        """Fail ``running_foreground`` leftovers from a crashed process.

        Cleans up dangling runs on startup; returns the failed agent ids.
        """
        failed: list[str] = []
        for record in self.list_instances():
            if record.status == "running_foreground":
                try:
                    self.update_instance(record.agent_id, status="failed")
                    failed.append(record.agent_id)
                except (OSError, ValueError):
                    continue
        return failed


def _record_from_dict(data: dict[str, Any]) -> SubagentInstanceRecord:
    status = str(data.get("status", ""))
    if status not in VALID_SUBAGENT_STATUSES:
        raise ValueError(f"Invalid subagent status: {status!r}")
    launch = data.get("launch_spec")
    spec = None
    if isinstance(launch, dict):
        spec = SubagentLaunchSpec(
            agent_id=str(launch.get("agent_id", "")),
            subagent_type=str(launch.get("subagent_type", "")),
            model_override=launch.get("model_override"),
            effective_model=launch.get("effective_model"),
            created_at=float(launch.get("created_at") or 0.0),
        )
    return SubagentInstanceRecord(
        agent_id=str(data.get("agent_id", "")),
        subagent_type=str(data.get("subagent_type", "")),
        status=status,
        description=str(data.get("description", "")),
        created_at=float(data.get("created_at") or 0.0),
        updated_at=float(data.get("updated_at") or 0.0),
        last_task_id=data.get("last_task_id"),
        launch_spec=spec,
    )
