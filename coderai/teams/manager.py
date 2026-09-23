"""TeamManager and TaskBoard for Agent Teams & Swarm Coordination."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from coderai.subagents.core import get_agent_registry
from coderai.teams.concurrency import ConcurrencyConflictError
from coderai.teams.deadlock import assert_acyclic_dependencies
from coderai.teams.mailbox import ActorChannel
from coderai.teams.models import TeamMessage, TeamTask, Teammate

logger = logging.getLogger(__name__)

MAX_TEAM_INBOX = 100


def is_acknowledgement_message(content: str) -> bool:
    """Return True if the message is an acknowledgement reply."""
    s = content.strip().lower()
    return s.startswith("acknowledged by") or s.startswith("ack by")


VALID_TASK_STATUSES = ("pending", "in_progress", "completed", "blocked", "failed")
VALID_TASK_PRIORITIES = ("low", "medium", "high", "critical")

REVIEWER_ROLE_NAMES = ("reviewer", "code-reviewer", "code_reviewer")

DEFAULT_REVIEWER_SYSTEM_PROMPT = (
    "You are a code reviewer teammate. Review diffs and code for correctness, "
    "security, and style. Report concrete findings; do not write code unless asked."
)


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
        deps = list(dependencies or [])

        # Validate that every dependency names a known task (dangling deps
        # would block dependents forever) and reject self-dependency.
        for dep_id in deps:
            if dep_id == task_id:
                raise ValueError(f"Task cannot depend on itself ('{task_id}').")
            if dep_id not in self._tasks:
                raise KeyError(f"Unknown task dependency '{dep_id}': no such task on the board.")

        # Validate DAG acyclicity
        candidate_deps = {t_id: list(t.dependencies or []) for t_id, t in self._tasks.items()}
        candidate_deps[task_id] = list(deps)
        assert_acyclic_dependencies(candidate_deps)

        task = TeamTask(
            task_id=task_id,
            title=title,
            description=description,
            assigned_to=assigned_to,
            priority=priority if priority in VALID_TASK_PRIORITIES else "medium",
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
            new_deps = list(dependencies)
            for dep_id in new_deps:
                if dep_id == task_id:
                    raise ValueError(f"Task cannot depend on itself ('{task_id}').")
                if dep_id not in self._tasks:
                    raise KeyError(
                        f"Unknown task dependency '{dep_id}': no such task on the board."
                    )
            candidate_deps = {t_id: list(t.dependencies or []) for t_id, t in self._tasks.items()}
            candidate_deps[task_id] = list(new_deps)
            assert_acyclic_dependencies(candidate_deps)
            task.dependencies = list(new_deps)

        if status:
            if status not in VALID_TASK_STATUSES:
                raise ValueError(
                    f"Unknown task status '{status}'. "
                    f"Valid statuses: {', '.join(VALID_TASK_STATUSES)}."
                )
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
        if not task:
            return False
        if not task.dependencies:
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
        project_root: str | None = None,
        parent_session_id: str | None = None,
        depth: int = 0,
    ) -> Teammate:
        teammate_id = f"tm_{uuid.uuid4().hex[:8]}"

        # Resolve markdown role specs if custom prompt not provided. The scan
        # root is clamped to a resolved directory (project root override wins)
        # so a stray process cwd cannot redirect role discovery.
        if not system_prompt:
            try:
                import os

                from pathlib import Path
                from coderai.subagents.registry import discover_markdown_agents

                scan_root = Path(
                    project_root or os.environ.get("CODERAI_PROJECT_ROOT") or str(Path.cwd())
                ).resolve()
                for d in discover_markdown_agents(scan_root):
                    if d.name.lower() == role.lower():
                        system_prompt = d.system_prompt or None
                        if not allowed_tools and d.tools:
                            allowed_tools = list(d.tools)
                        if mode == "general" and d.mode:
                            mode = d.mode
                        break
            except Exception:
                pass

        if not system_prompt and role.strip().lower() in REVIEWER_ROLE_NAMES:
            system_prompt = DEFAULT_REVIEWER_SYSTEM_PROMPT

        teammate = Teammate(
            teammate_id=teammate_id,
            name=name,
            role=role,
            mode=mode if mode in ("read_only", "general") else "general",
            system_prompt=system_prompt,
            allowed_tools=allowed_tools,
            status="idle",
            project_root=project_root,
            parent_session_id=parent_session_id,
            depth=depth,
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
                tm.unread_messages.append(msg)
                if len(tm.inbox) > MAX_TEAM_INBOX:
                    tm.inbox = tm.inbox[-MAX_TEAM_INBOX:]
                if len(tm.unread_messages) > MAX_TEAM_INBOX:
                    tm.unread_messages = tm.unread_messages[-MAX_TEAM_INBOX:]
                mb = self.channel.get_mailbox(tm.teammate_id)
                if mb:
                    if not mb.send_nowait(msg):
                        logger.warning(
                            "Teammate mailbox full for '%s'; message %s dropped.",
                            tm.teammate_id,
                            msg.message_id,
                        )
        else:
            target = self.get_teammate(recipient)
            if target:
                target.inbox.append(msg)
                target.unread_messages.append(msg)
                if len(target.inbox) > MAX_TEAM_INBOX:
                    target.inbox = target.inbox[-MAX_TEAM_INBOX:]
                if len(target.unread_messages) > MAX_TEAM_INBOX:
                    target.unread_messages = target.unread_messages[-MAX_TEAM_INBOX:]
                mb = self.channel.get_mailbox(target.teammate_id)
                if mb:
                    if not mb.send_nowait(msg):
                        logger.warning(
                            "Teammate mailbox full for '%s'; message %s dropped.",
                            target.teammate_id,
                            msg.message_id,
                        )
            else:
                logger.warning(
                    "send_message: unknown recipient '%s'; message %s not delivered.",
                    recipient,
                    msg.message_id,
                )

        sender_tm = self.get_teammate(sender)
        if sender_tm:
            sender_tm.outbox.append(msg)
            if len(sender_tm.outbox) > MAX_TEAM_INBOX:
                sender_tm.outbox = sender_tm.outbox[-MAX_TEAM_INBOX:]

        return msg

    def get_messages(self, teammate_id: str, mark_read: bool = True) -> list[TeamMessage]:
        tm = self.get_teammate(teammate_id)
        if not tm:
            return []
        msgs = list(tm.inbox)
        if mark_read:
            tm.unread_messages.clear()
        return msgs

    async def wait_agent(
        self,
        agent_ids: list[str] | str,
        timeout_seconds: float = 60.0,
        wait_for: str = "completion",  # "completion" | "message" | "any_settlement"
    ) -> dict[str, Any]:
        """Await completion or message settlement from spawned teammates or subagents."""
        if wait_for not in ("completion", "message", "any_settlement"):
            raise ValueError(
                f"Unknown wait_for mode '{wait_for}'. "
                "Expected 'completion', 'message', or 'any_settlement'."
            )
        if timeout_seconds < 0:
            raise ValueError(f"timeout_seconds must be >= 0, got {timeout_seconds}.")
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
                    has_msg = len(tm.unread_messages) > 0

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

    def get_runnable_tasks_for_teammate(self, teammate_id: str) -> list[TeamTask]:
        tm = self.get_teammate(teammate_id)
        if not tm:
            return []
        candidate_tasks = self.task_board.list_tasks(
            assigned_to=tm.teammate_id
        ) + self.task_board.list_tasks(assigned_to=tm.name)
        seen: set[str] = set()
        runnable: list[TeamTask] = []
        for t in candidate_tasks:
            if t.task_id not in seen:
                seen.add(t.task_id)
                if t.status in ("pending", "in_progress") and self.task_board.can_start_task(
                    t.task_id
                ):
                    runnable.append(t)
        return runnable

    async def _execute_task(self, teammate: Teammate, task: TeamTask) -> str:
        """Execute task assigned to a teammate.

        Returns the sub-agent's summary report on success. Raises
        RuntimeError when the sub-agent fails, is misconfigured, or produces
        no report — callers must mark the task ``failed``, never ``completed``.
        """
        import os
        from pathlib import Path

        from coderai.llm import create_openai_client
        from coderai.subagents.builder import build_spec
        from coderai.subagents.runner import SubAgentManager

        prompt = (
            f"You are teammate {teammate.name} with role '{teammate.role}'.\n"
            f"Task Title: {task.title}\n"
            f"Task Description: {task.description}\n"
            f"Priority: {task.priority}\n"
        )
        spec = build_spec(
            context=None,
            args={
                "description": task.title,
                "prompt": prompt,
                "subagent_type": teammate.role,
                "mode": teammate.mode,
            },
            allowed_tools=teammate.allowed_tools,
            system_prompt=teammate.system_prompt,
            parent_session_id=teammate.parent_session_id,
            depth=teammate.depth + 1,
        )
        exec_root = (
            teammate.project_root or os.environ.get("CODERAI_PROJECT_ROOT") or str(Path.cwd())
        )
        runner = SubAgentManager(
            project_root=exec_root,
            create_openai_client=lambda: create_openai_client(exec_root),
        )
        result = await runner.spawn_subagent(spec)
        if result.status == "completed" and result.summary:
            return result.summary
        raise RuntimeError(
            f"Sub-agent for team task '{task.title}' ended with status "
            f"'{result.status}': {result.error or result.summary or 'no details'}"
        )

    async def _teammate_worker(self, teammate_id: str) -> None:
        """Autonomous worker loop for active teammates in the swarm."""
        mb = self.channel.get_mailbox(teammate_id)
        while True:
            try:
                tm = self.get_teammate(teammate_id)
                if not tm:
                    break

                # 1. Check for tasks assigned to this teammate that are ready
                runnable_tasks = self.get_runnable_tasks_for_teammate(teammate_id)
                if runnable_tasks:
                    task = runnable_tasks[0]
                    if task.status == "pending":
                        self.task_board.update_task(task.task_id, status="in_progress")
                    tm.status = "working"
                    try:
                        report = await self._execute_task(tm, task)
                    except Exception as exc:
                        # Fail honestly: a task whose sub-agent errored is
                        # `failed`, never `completed` with a synthetic report.
                        # Dependents stay blocked via can_start_task(), and
                        # wait_agent() treats `failed` as settled.
                        err = str(exc) or "unknown error"
                        logger.warning("Team task '%s' failed for %s: %s", task.title, tm.name, err)
                        self.task_board.update_task(
                            task.task_id,
                            status="failed",
                            result=err,
                            notes=f"teammate={tm.name}",
                        )
                        tm.status = "failed"
                        tm.last_report = err
                        self.send_message(
                            sender=tm.name,
                            recipient="all",
                            content=f"Task '{task.title}' failed for {tm.name}: {err}",
                            task_id=task.task_id,
                        )
                        await asyncio.sleep(0.05)
                        continue
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
                            if not is_acknowledgement_message(msg.content):
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
            except Exception:
                logger.warning(
                    "Teammate %s worker loop error; continuing watch loop.",
                    teammate_id,
                    exc_info=True,
                )
                await asyncio.sleep(0.5)

    def cancel_all_teammates(self) -> None:
        """Cancel and clean up all active teammate worker tasks."""

        def _consume_late_failure(done: asyncio.Task[Any]) -> None:
            # Swallow late worker failures so a cancelled loop never
            # surfaces "Task exception was never retrieved" warnings.
            try:
                if not done.cancelled():
                    done.exception()
            except Exception:
                pass

        for teammate_id, task in list(self._active_tasks.items()):
            try:
                if not task.done():
                    task.cancel()
                task.add_done_callback(_consume_late_failure)
            except Exception:
                logger.warning("Failed to cancel teammate worker '%s'.", teammate_id, exc_info=True)
            try:
                exc = task.exception() if task.done() else None
                if exc is not None:
                    logger.warning("Teammate worker '%s' ended with error: %s", teammate_id, exc)
            except (asyncio.CancelledError, Exception):
                pass
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
