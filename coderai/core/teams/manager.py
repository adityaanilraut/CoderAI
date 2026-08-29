"""TeamManager and TaskBoard for Agent Teams & Swarm Coordination."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from coderai.core.agents import get_agent_registry
from coderai.core.teams.concurrency import ConcurrencyConflictError
from coderai.core.teams.deadlock import assert_acyclic_dependencies
from coderai.core.teams.mailbox import ActorChannel, AsyncMailbox, MessagePriority
from coderai.core.teams.models import TeamMessage, TeamTask, Teammate

logger = logging.getLogger(__name__)


class TeamTaskBoard:
    """Shared task board for multi-agent coordination and dependency tracking."""

    def __init__(self) -> None:
        self._tasks: dict[str, TeamTask] = {}

    def _validate_dag(self) -> None:
        """Validate that all registered tasks and their dependencies form a strict DAG."""
        dep_map = {t_id: list(t.dependencies or []) for t_id, t in self._tasks.items()}
        assert_acyclic_dependencies(dep_map)

    def create_task(
        self,
        title: str,
        description: str,
        assigned_to: str | None = None,
        priority: str = "medium",
        dependencies: list[str] | None = None,
    ) -> TeamTask:
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        deps = dependencies or []

        # Validate DAG acyclicity
        candidate_deps = {t_id: list(t.dependencies or []) for t_id, t in self._tasks.items()}
        candidate_deps[task_id] = list(deps)
        assert_acyclic_dependencies(candidate_deps)

        task = TeamTask(
            task_id=task_id,
            title=title,
            description=description,
            assigned_to=assigned_to,
            priority=priority if priority in ("low", "medium", "high", "critical") else "medium",
            dependencies=deps,
        )
        self._tasks[task_id] = task
        return task

    def get_task(self, task_id: str) -> TeamTask | None:
        return self._tasks.get(task_id)

    def list_tasks(
        self,
        status: str | None = None,
        assigned_to: str | None = None,
    ) -> list[TeamTask]:
        tasks = list(self._tasks.values())
        if status:
            tasks = [t for t in tasks if t.status == status]
        if assigned_to:
            tasks = [t for t in tasks if t.assigned_to == assigned_to]
        return tasks

    def update_task(
        self,
        task_id: str,
        status: str | None = None,
        assigned_to: str | None = None,
        result: str | None = None,
        notes: str | None = None,
        dependencies: list[str] | None = None,
        expected_revision: int | None = None,
    ) -> TeamTask:
        task = self._tasks.get(task_id)
        if not task:
            raise KeyError(f"Task '{task_id}' not found on team task board.")
        if expected_revision is not None and task.revision != expected_revision:
            raise ConcurrencyConflictError(
                f"ConcurrencyConflictError: Task '{task_id}' revision mismatch "
                f"(expected revision {expected_revision}, but current is {task.revision}). Refresh task state before updating.",
                resource_id=task_id,
                expected_revision=expected_revision,
                actual_revision=task.revision,
            )

        if dependencies is not None:
            candidate_deps = {t_id: list(t.dependencies or []) for t_id, t in self._tasks.items()}
            candidate_deps[task_id] = list(dependencies)
            assert_acyclic_dependencies(candidate_deps)
            task.dependencies = list(dependencies)

        if status:
            task.status = status
        if assigned_to is not None:
            task.assigned_to = assigned_to
        if result is not None:
            task.result = result
        if notes is not None:
            task.notes = notes
        task.revision += 1
        task.updated_at = time.time()
        return task

    def can_start_task(self, task_id: str) -> bool:
        """Check if all dependencies for a task are completed."""
        task = self._tasks.get(task_id)
        if not task or not task.dependencies:
            return True
        for dep_id in task.dependencies:
            dep = self._tasks.get(dep_id)
            if not dep or dep.status != "completed":
                return False
        return True


class TeamManager:
    """Manages team membership, message routing, and swarm execution."""

    def __init__(self) -> None:
        self.task_board = TeamTaskBoard()
        self.channel = ActorChannel()
        self._teammates: dict[str, Teammate] = {}
        self._active_tasks: dict[str, asyncio.Task[Any]] = {}

    def spawn_teammate(
        self,
        name: str,
        role: str,
        system_prompt: str | None = None,
        mode: str = "general",
        allowed_tools: list[str] | None = None,
    ) -> Teammate:
        teammate_id = f"tm_{uuid.uuid4().hex[:8]}"
        teammate = Teammate(
            teammate_id=teammate_id,
            name=name,
            role=role,
            mode=mode if mode in ("read_only", "general") else "general",
            system_prompt=system_prompt,
            allowed_tools=allowed_tools,
            status="idle",
        )
        self._teammates[teammate_id] = teammate
        self.channel.register_mailbox(teammate_id)
        return teammate

    def get_teammate(self, identifier: str) -> Teammate | None:
        """Look up a teammate by ID or name."""
        if identifier in self._teammates:
            return self._teammates[identifier]
        for tm in self._teammates.values():
            if tm.name.lower() == identifier.lower():
                return tm
        return None

    def list_teammates(self) -> list[Teammate]:
        return list(self._teammates.values())

    def send_message(
        self,
        sender: str,
        recipient: str,
        content: str,
        task_id: str | None = None,
    ) -> TeamMessage:
        msg = TeamMessage(
            sender=sender,
            recipient=recipient,
            content=content,
            task_id=task_id,
        )
        if recipient == "all":
            for tm in self._teammates.values():
                tm.inbox.append(msg)
                mb = self.channel.get_mailbox(tm.teammate_id)
                if mb:
                    mb.send_nowait(msg)
        else:
            target = self.get_teammate(recipient)
            if target:
                target.inbox.append(msg)
                mb = self.channel.get_mailbox(target.teammate_id)
                if mb:
                    mb.send_nowait(msg)

        sender_tm = self.get_teammate(sender)
        if sender_tm:
            sender_tm.outbox.append(msg)

        return msg

    def get_messages(self, teammate_id: str) -> list[TeamMessage]:
        tm = self.get_teammate(teammate_id)
        return list(tm.inbox) if tm else []

    async def wait_agent(
        self,
        agent_ids: list[str] | str,
        timeout_seconds: float = 60.0,
        wait_for: str = "completion",  # "completion" | "message" | "any_settlement"
    ) -> dict[str, Any]:
        """Await completion or message settlement from spawned teammates or subagents."""
        if isinstance(agent_ids, str):
            target_ids = [agent_ids]
        else:
            target_ids = list(agent_ids)

        if not target_ids:
            return {"ok": True, "status": "no_agents", "agents": []}

        start_time = time.time()
        agent_registry = get_agent_registry()

        while True:
            all_settled = True
            agent_statuses: list[dict[str, Any]] = []

            for aid in target_ids:
                # Check in TeamManager
                tm = self.get_teammate(aid)
                if tm:
                    is_done = tm.status in ("completed", "failed", "interrupted")
                    has_msg = len(tm.inbox) > 0

                    if wait_for == "completion":
                        settled = is_done
                    elif wait_for == "message":
                        settled = has_msg
                    else:  # any_settlement
                        settled = is_done or has_msg

                    if not settled:
                        all_settled = False

                    agent_statuses.append(
                        {
                            "id": tm.teammate_id,
                            "name": tm.name,
                            "role": tm.role,
                            "status": tm.status,
                            "settled": settled,
                            "inbox_count": len(tm.inbox),
                            "last_report": tm.last_report,
                        }
                    )
                    continue

                # Check in AgentRegistry
                handle = agent_registry.get(aid)
                if handle:
                    is_done = handle.status in ("completed", "failed", "interrupted", "timeout")
                    has_msg = len(handle.inbox) > 0

                    if wait_for == "completion":
                        settled = is_done
                    elif wait_for == "message":
                        settled = has_msg
                    else:
                        settled = is_done or has_msg

                    if not settled:
                        all_settled = False

                    agent_statuses.append(
                        {
                            "id": handle.id,
                            "description": handle.description,
                            "status": handle.status,
                            "settled": settled,
                            "inbox_count": len(handle.inbox),
                            "report": handle.report,
                        }
                    )
                    continue

                # Agent not found
                agent_statuses.append(
                    {
                        "id": aid,
                        "status": "not_found",
                        "settled": True,
                    }
                )

            if all_settled:
                return {
                    "ok": True,
                    "status": "settled",
                    "elapsed_seconds": max(0.0, time.time() - start_time),
                    "agents": agent_statuses,
                }

            if time.time() - start_time >= timeout_seconds:
                return {
                    "ok": False,
                    "status": "timeout",
                    "elapsed_seconds": timeout_seconds,
                    "agents": agent_statuses,
                }

            await asyncio.sleep(0.5)


_global_team_manager = TeamManager()


def get_team_manager() -> TeamManager:
    """Get the global default TeamManager instance."""
    return _global_team_manager


def reset_team_manager() -> None:
    """Reset the global TeamManager instance (useful for test isolation)."""
    global _global_team_manager
    _global_team_manager = TeamManager()
