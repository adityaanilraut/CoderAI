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
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from coderai.utils.storage import (
    owned_path,
    storage_id,
    read_bytes,
    write_bytes,
    remove_path,
    storage_lock,
    parent_directory,
)
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
    owner_pid: int | None = None


def new_agent_id(prefix: str = "agt") -> str:
    """Fresh instance id (``agt_<32 hex>``)."""
    return f"{prefix}_{uuid.uuid4().hex}"


class SubagentStore:
    """Filesystem-backed instance registry scoped to one session."""

    def __init__(self, session_dir: str | Path) -> None:
        raw = Path(session_dir)
        self._session_dir = raw.parent.resolve() / raw.name
        if self._session_dir.is_symlink():
            raise ValueError("Session storage must not be a symlink")

    # -- paths ----------------------------------------------------------
    @property
    def root(self) -> Path:
        return owned_path(self._session_dir, "subagents")

    def instance_dir(self, agent_id: str, *, create: bool = False) -> Path:
        path = owned_path(self.root, storage_id(agent_id))
        if create:
            with parent_directory(path / "meta.json", create=True):
                pass
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
            write_bytes(self.instance_dir(agent_id) / name, b"", append=True)
        record = SubagentInstanceRecord(
            agent_id=agent_id,
            subagent_type=subagent_type,
            status="idle",
            description=description,
            created_at=now,
            updated_at=now,
            owner_pid=os.getpid(),
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
        import json

        write_bytes(self.meta_path(record.agent_id), json.dumps(asdict(record)).encode("utf-8"))

    def update_instance(
        self,
        agent_id: str,
        *,
        status: str | None = None,
        last_task_id: str | None = None,
    ) -> SubagentInstanceRecord | None:
        """Patch status/task fields (validates status; returns updated record)."""
        with storage_lock(self.meta_path(agent_id)):
            return self._update_instance(agent_id, status=status, last_task_id=last_task_id)

    def _update_instance(
        self, agent_id: str, *, status: str | None, last_task_id: str | None
    ) -> SubagentInstanceRecord | None:
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
            owner_pid=record.owner_pid,
        )
        self.write_instance(updated)
        return updated

    def write_prompt(self, agent_id: str, prompt: str) -> None:
        """Persist the launch prompt (best-effort)."""
        try:
            self.instance_dir(agent_id, create=True)
            write_bytes(self.prompt_path(agent_id), prompt.encode("utf-8"))
        except OSError as exc:
            logger.debug("Subagent prompt write failed: {error}", error=exc)

    def write_context(self, agent_id: str, messages: list[dict[str, Any]]) -> None:
        """Atomically replace a child's independent conversation checkpoint."""
        import json

        content = "".join(json.dumps(message, ensure_ascii=False) + "\n" for message in messages)
        write_bytes(self.context_path(agent_id), content.encode("utf-8"))

    def write_output(self, agent_id: str, output: str) -> None:
        write_bytes(self.output_path(agent_id), output.encode("utf-8"))

    # -- reads ----------------------------------------------------------
    def get_instance(self, agent_id: str) -> SubagentInstanceRecord | None:
        meta = self.meta_path(agent_id)
        if not meta.is_file():
            return None
        try:
            import json

            data = json.loads(read_bytes(meta, limit=1_000_000))
            if not isinstance(data, dict) or data.get("agent_id") != agent_id:
                raise ValueError("Invalid subagent record identity")
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
            if child.is_dir() and not child.is_symlink():
                record = self.get_instance(child.name)
                if record is not None:
                    records.append(record)
        return records

    def delete_instance(self, agent_id: str) -> None:
        remove_path(self.instance_dir(agent_id), tree=True)

    def recover_stale_instances(self, live_agent_ids: set[str]) -> list[str]:
        """Fail interrupted native runs, preserving workers owned by live processes."""
        failed = []
        for record in self.list_instances():
            if (
                record.status not in {"running_foreground", "running_background"}
                or record.agent_id in live_agent_ids
            ):
                continue
            if record.owner_pid is not None and record.owner_pid != os.getpid():
                try:
                    os.kill(record.owner_pid, 0)
                    continue
                except PermissionError:
                    continue
                except ProcessLookupError:
                    pass
            self.update_instance(record.agent_id, status="failed")
            failed.append(record.agent_id)
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
        owner_pid=data.get("owner_pid")
        if isinstance(data.get("owner_pid"), int) and data["owner_pid"] > 0
        else None,
    )
