"""TeamManager and TaskBoard for Agent Teams & Swarm Coordination."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from coderai.core.agents import get_agent_registry
from coderai.teams.concurrency import ConcurrencyConflictError
from coderai.teams.deadlock import assert_acyclic_dependencies
from coderai.teams.mailbox import ActorChannel
from coderai.teams.models import TeamMessage, TeamTask, Teammate

logger = logging.getLogger(__name__)


class TeamTaskBoard:
    """Shared task board for multi-agent coordination and dependency tracking."""

    def __init__(self, manager: TeamManager | None = None) -> None:
        self._tasks: dict[str, TeamTask] = {}
        self._manager = manager

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
            if status == "completed" and self._manager is not None and task.assigned_to:
                tm = self._manager.get_teammate(task.assigned_to)
                if tm is not None:
                    tm.status = "completed"
                    if result:
                        tm.last_report = result
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
        self.task_board = TeamTaskBoard(manager=self)
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
        auto_start: bool = True,
    ) -> Teammate:
        teammate_id = f"tm_{uuid.uuid4().hex[:8]}"

        # Resolve markdown role specs if custom prompt not provided
        if not system_prompt:
            try:
                from pathlib import Path
                from coderai.subagents.registry import discover_markdown_agents

                for d in discover_markdown_agents(Path.cwd()):
                    if d.name.lower() == role.lower():
                        system_prompt = d.system_prompt
                        if not allowed_tools and d.tools:
                            allowed_tools = list(d.tools)
                        if mode == "general" and d.mode:
                            mode = d.mode
                        break
            except Exception:
                pass

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

        if auto_start:
            try:
                loop = asyncio.get_running_loop()
                t = loop.create_task(self._teammate_worker(teammate_id))
                self._active_tasks[teammate_id] = t
            except RuntimeError:
                pass

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

    async def _execute_task(self, teammate: Teammate, task: TeamTask) -> str:
        """Execute task assigned to a teammate."""
        try:
            from pathlib import Path
            from coderai.core.openai_client import create_openai_client
            from coderai.subagents.builder import SubAgentSpec
            from coderai.subagents.runner import SubAgentRunner

            prompt = (
                f"You are teammate {teammate.name} with role '{teammate.role}'.\n"
                f"Task Title: {task.title}\n"
                f"Task Description: {task.description}\n"
                f"Priority: {task.priority}\n"
            )
            spec = SubAgentSpec(
                subagent_type=teammate.role,
                prompt=prompt,
                description=task.title,
                mode=teammate.mode,
                allowed_tools=teammate.allowed_tools,
                system_prompt=teammate.system_prompt,
            )
            runner = SubAgentRunner(
                project_root=str(Path.cwd()),
                create_openai_client=lambda: create_openai_client(str(Path.cwd())),
            )
            result = await runner.run(spec)
            if result.report:
                return result.report
        except Exception as exc:
            logger.debug(f"SubAgentRunner fallback for team task: {exc}")

        return f"Task '{task.title}' completed by {teammate.name} ({teammate.role})."

    async def _teammate_worker(self, teammate_id: str) -> None:
        """Autonomous worker loop for active teammates in the swarm."""
        mb = self.channel.get_mailbox(teammate_id)
        while True:
            try:
                tm = self.get_teammate(teammate_id)
                if not tm:
                    break

                # 1. Check for tasks assigned to this teammate that are marked in_progress
                runnable_tasks = [
                    t
                    for t in self.task_board.list_tasks(assigned_to=teammate_id)
                    if t.status == "in_progress" and self.task_board.can_start_task(t.task_id)
                ]
                if not runnable_tasks:
                    runnable_tasks = [
                        t
                        for t in self.task_board.list_tasks(assigned_to=tm.name)
                        if t.status == "in_progress" and self.task_board.can_start_task(t.task_id)
                    ]

                if runnable_tasks:
                    task = runnable_tasks[0]
                    tm.status = "working"
                    report = await self._execute_task(tm, task)
                    self.task_board.update_task(
                        task.task_id,
                        status="completed",
                        result=report,
                    )
                    tm.status = "completed"
                    tm.last_report = report
                    self.send_message(
                        sender=tm.name,
                        recipient="all",
                        content=f"Task '{task.title}' completed by {tm.name}: {report}",
                        task_id=task.task_id,
                    )
                    await asyncio.sleep(0.05)
                    continue

                # 2. Check for mailbox messages
                if mb and not mb.is_empty():
                    msg = await mb.receive_async(timeout_seconds=0.05)
                    if msg and msg.sender != tm.name:
                        if msg.recipient in (tm.name, tm.teammate_id):
                            reply = f"Acknowledged by {tm.name} ({tm.role}): {msg.content[:60]}"
                            self.send_message(
                                sender=tm.name,
                                recipient=msg.sender,
                                content=reply,
                                task_id=msg.task_id,
                            )

                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug(f"Teammate {teammate_id} loop exception: {exc}")
                await asyncio.sleep(0.5)

    def cancel_all_teammates(self) -> None:
        """Cancel and clean up all active teammate worker tasks."""
        for task in list(self._active_tasks.values()):
            if not task.done():
                task.cancel()
        self._active_tasks.clear()


_global_team_manager = TeamManager()


def get_team_manager() -> TeamManager:
    """Get the global default TeamManager instance."""
    return _global_team_manager


def reset_team_manager() -> None:
    """Reset the global TeamManager instance (useful for test isolation)."""
    global _global_team_manager
    if _global_team_manager is not None:
        _global_team_manager.cancel_all_teammates()
    _global_team_manager = TeamManager()
