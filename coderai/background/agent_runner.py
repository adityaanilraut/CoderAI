# Ported from coderai/core/agents.py - kimi structure (background/agent_runner.py).
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from coderai.core.orchestration import (
    DEFAULT_MAX_CONTINUABLE_AGENTS,
    publish_subagent_end,
    publish_subagent_start,
    resolve_max_continuable_agents,
)
from coderai.subagents.builder import SubAgentSpec
from coderai.subagents.core import (
    AgentHandle,
    AgentRegistry,
    append_parent_session_notice,
    get_agent_registry,
    notify_parent_session,
)
from coderai.subagents.output import SubAgentResult
from coderai.subagents.runner import SubAgentManager

class TaskSupervisor:
    """Supervises long-running background tasks, subagents, and jobs with liveness monitoring and cleanup."""

    def __init__(self, agent_registry: AgentRegistry | None = None) -> None:
        self._registry = agent_registry or get_agent_registry()
        self._heartbeats: dict[str, float] = {}

    def record_heartbeat(self, task_id: str) -> bool:
        """Record liveness heartbeat timestamp for a task or agent."""
        now = time.time()
        self._heartbeats[task_id] = now
        handle = self._registry.get(task_id)
        if handle:
            handle.last_heartbeat_at = now
            return True
        return False

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """Inspect task status and metadata across agents and background jobs."""
        handle = self._registry.get(task_id)
        if handle:
            return {
                "id": handle.id,
                "type": "subagent",
                "session_id": handle.parent_session_id,
                "description": handle.description,
                "mode": handle.mode,
                "status": handle.status,
                "depth": handle.depth,
                "started_at": handle.started_at,
                "last_heartbeat_at": handle.last_heartbeat_at,
                "parent_agent_id": handle.parent_agent_id,
                "children_ids": list(handle.children_ids),
                "has_result": handle.result is not None,
                "report": handle.report,
                "summary": handle.report or (handle.result.summary if handle.result else None),
            }

        from coderai.background.manager import get_job_store

        job_store = get_job_store()
        with job_store._lock:
            job = job_store._jobs.get(task_id)
            if job:
                return {
                    "id": job.id,
                    "type": "job",
                    "kind": job.kind,
                    "session_id": job.session_id,
                    "label": job.label,
                    "status": job.status,
                    "started_at": job.started_at / 1000.0,
                    "last_heartbeat_at": job.started_at / 1000.0,
                    "detail": job.detail,
                }
        return None

    def list_tasks(
        self, session_id: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        """List active and historical background tasks and subagents."""
        tasks: list[dict[str, Any]] = []
        for handle in self._registry.list(parent_session_id=session_id):
            if status and handle.status != status:
                continue
            tasks.append(
                {
                    "id": handle.id,
                    "type": "subagent",
                    "session_id": handle.parent_session_id,
                    "description": handle.description,
                    "mode": handle.mode,
                    "status": handle.status,
                    "depth": handle.depth,
                    "started_at": handle.started_at,
                    "last_heartbeat_at": handle.last_heartbeat_at,
                    "parent_agent_id": handle.parent_agent_id,
                    "children_ids": list(handle.children_ids),
                    "report": handle.report,
                    "summary": handle.report or (handle.result.summary if handle.result else None),
                }
            )

        from coderai.background.manager import get_job_store

        job_store = get_job_store()
        with job_store._lock:
            for job in job_store._jobs.values():
                if session_id and job.session_id != session_id:
                    continue
                if status and job.status != status:
                    continue
                tasks.append(
                    {
                        "id": job.id,
                        "type": "job",
                        "kind": job.kind,
                        "session_id": job.session_id,
                        "label": job.label,
                        "status": job.status,
                        "started_at": job.started_at / 1000.0,
                        "last_heartbeat_at": job.started_at / 1000.0,
                        "detail": job.detail,
                    }
                )
        return sorted(tasks, key=lambda t: t.get("started_at", 0))

    def kill_task(self, task_id: str, reason: str | None = None) -> bool:
        """Cancel and terminate a background task or subagent."""
        handle = self._registry.get(task_id)
        if handle:
            self._registry.interrupt(task_id)
            return True

        from coderai.background.manager import get_job_store

        job_store = get_job_store()
        with job_store._lock:
            job = job_store._jobs.get(task_id)
            if job:
                job_store.kill(task_id, job.session_id, reason=reason or "Supervisor killed task")
                return True
        return False

    def kill_all_tasks(self, session_id: str | None = None) -> list[str]:
        """Cancel all running background tasks for a session or globally."""
        killed: list[str] = []
        for handle in self._registry.list(parent_session_id=session_id):
            if handle.status == "running":
                self._registry.interrupt(handle.id)
                killed.append(handle.id)

        from coderai.background.manager import get_job_store

        job_store = get_job_store()
        with job_store._lock:
            for job in list(job_store._jobs.values()):
                if session_id and job.session_id != session_id:
                    continue
                if job.status == "running":
                    job_store.kill(job.id, job.session_id, reason="Supervisor bulk kill")
                    killed.append(job.id)
        return killed

    def check_liveness(self, idle_timeout_seconds: float = 60.0) -> list[str]:
        """Reap tasks that have stopped sending heartbeats within the idle timeout limit."""
        now = time.time()
        reaped: list[str] = []
        for handle in self._registry.list():
            if handle.status == "running":
                last_hb = handle.last_heartbeat_at or handle.started_at or now
                if now - last_hb > idle_timeout_seconds:
                    handle.status = "timeout"
                    if handle.task and not handle.task.done():
                        handle.task.cancel()
                    reaped.append(handle.id)
        return reaped

    def cleanup_session_tasks(self, session_id: str) -> list[str]:
        """Clean up and cancel all background tasks associated with a dropped session."""
        return self.kill_all_tasks(session_id=session_id)


_supervisor = TaskSupervisor(get_agent_registry())


def get_task_supervisor() -> TaskSupervisor:
    return _supervisor


def new_agent_id() -> str:
    return f"agent_{uuid.uuid4().hex[:8]}"


async def spawn_background_agent(
    manager: SubAgentManager,
    spec: SubAgentSpec,
    parent_agent_id: str | None = None,
) -> AgentHandle:
    registry = get_agent_registry()
    agent_id = new_agent_id()
    spec.agent_id = agent_id
    spec.continuable = True

    # Lineage propagation
    parent_handle = registry.get(parent_agent_id) if parent_agent_id else None
    if parent_handle:
        spec.parent_agent_id = parent_handle.id
        spec.root_agent_id = parent_handle.root_agent_id or parent_handle.id
        spec.depth = parent_handle.depth + 1
        if agent_id not in parent_handle.children_ids:
            parent_handle.children_ids.append(agent_id)
    elif spec.parent_agent_id:
        p_handle = registry.get(spec.parent_agent_id)
        if p_handle:
            spec.root_agent_id = p_handle.root_agent_id or p_handle.id
            spec.depth = p_handle.depth + 1
            if agent_id not in p_handle.children_ids:
                p_handle.children_ids.append(agent_id)
    else:
        spec.root_agent_id = agent_id

    # Per-session spawn cap (configurable via CODERAI_MAX_CONTINUABLE_AGENTS_PER_SESSION).
    max_continuable = resolve_max_continuable_agents()
    siblings = [
        h
        for h in registry.list(parent_session_id=spec.parent_session_id)
        if h.id != agent_id and h.status in ("running", "interrupted")
    ]
    if len(siblings) >= max_continuable:
        raise RuntimeError(
            f"subagent spawn cap reached: at most {max_continuable} "
            "live sub-agents per session (MAX_CONTINUABLE_AGENTS_PER_SESSION)"
        )

    now = time.time()
    handle = AgentHandle(
        id=agent_id,
        parent_session_id=spec.parent_session_id or "",
        description=spec.description,
        mode=spec.mode,
        spec=spec,
        parent_agent_id=spec.parent_agent_id,
        root_agent_id=spec.root_agent_id,
        depth=spec.depth,
        children_ids=[],
        started_at=now,
        last_heartbeat_at=now,
        inbox_waiter=asyncio.Event(),
        manager=manager,
        run_session_id=(
            f"sub_{spec.parent_session_id[:8] if spec.parent_session_id else 'root'}_{spec.task_id}"
        ),
    )
    spec.handle = handle
    registry.register(handle)

    def _parent_notice(session_id: str, text: str) -> None:
        if notify_parent_session(session_id, text):
            return
        append_parent_session_notice(getattr(manager, "project_root", "."), session_id, text)

    handle.parent_notice = _parent_notice

    async def _run() -> None:
        run_id = uuid.uuid4().hex
        session_id = handle.run_session_id or ""
        provider = spec.provider or "in_process"
        publish_subagent_start(
            run_id=run_id,
            provider=provider,
            child_id=agent_id,
            local=provider == "in_process",
            parent_session_id=spec.parent_session_id,
        )
        try:
            result = await manager.run_continuable(spec, session_id)
            handle.result = result
            if handle.status != "interrupted":
                handle.status = result.status
            handle.last_stop_reason = result.stop_reason
        except asyncio.CancelledError:
            handle.status = "interrupted"
            handle.last_stop_reason = "aborted"
            handle.result = SubAgentResult(
                task_id=spec.task_id,
                session_id=session_id,
                status="interrupted",
                summary="Sub-agent was cancelled.",
                error="CancelledError: Parent or runner cancelled sub-agent.",
                exit_code=130,
                parent_agent_id=spec.parent_agent_id,
                root_agent_id=spec.root_agent_id,
                depth=spec.depth,
                children_ids=list(spec.children_ids),
            )
        except Exception as exc:
            handle.status = "failed"
            handle.last_stop_reason = "error"
            handle.result = SubAgentResult(
                task_id=spec.task_id,
                session_id=session_id,
                status="failed",
                summary=str(exc),
                error=str(exc),
                parent_agent_id=spec.parent_agent_id,
                root_agent_id=spec.root_agent_id,
                depth=spec.depth,
                children_ids=list(spec.children_ids),
            )
        finally:
            publish_subagent_end(
                run_id=run_id,
                provider=provider,
                child_id=agent_id,
                local=provider == "in_process",
                stop_reason=handle.last_stop_reason or "aborted",
                last_assistant_message=(
                    [{"type": "text", "text": handle.result.summary}]
                    if handle.result and handle.result.summary
                    else None
                ),
                parent_session_id=spec.parent_session_id,
            )

    handle.task = asyncio.create_task(_run())
    return handle
