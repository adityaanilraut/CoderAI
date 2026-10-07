"""Session-scoped async shell lifecycle and sole interactive input owner."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
import json
from typing import Any, Literal
import uuid

from coderai.ui.shell.interaction import BrowserPosition, BrowserRow, choose, inspect_output
from coderai.ui.shell.preferences import DisplayPreferences
from coderai.ui.shell.presentation import ShellPresentation
from coderai.ui.shell.submission import (
    PromptSubmission,
    QuestionOutcome,
    prepare_submission,
)


class ShellState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    AWAITING_APPROVAL = "approval"
    AWAITING_ANSWER = "answer"
    CANCELLING = "cancelling"
    FAILED = "failed"
    CLOSED = "closed"


@dataclass
class ShellViewState:
    phase: ShellState = ShellState.IDLE
    session_id: str | None = None
    plan_mode: bool = False
    draft: str = ""
    cursor: int | None = None
    queue_paused: bool = False
    queue: list[PromptSubmission] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    last_failed: PromptSubmission | None = None
    pending_skills: list[str] = field(default_factory=list)


class ShellController:
    def __init__(
        self,
        mgr: Any,
        console: Any = None,
        *,
        yes: bool = False,
        plan_mode: bool = False,
        prompt: Any = None,
    ) -> None:
        from coderai.ui.shell.prompt import CoderAIPromptSession

        self.mgr, self.console, self.yes = mgr, console, yes
        self.view = ShellViewState(plan_mode=plan_mode)
        self.preferences = DisplayPreferences.load(mgr.project_root)
        from coderai.ui.theme import set_active_theme

        set_active_theme(self.preferences.theme)
        self.prompt = prompt or CoderAIPromptSession(
            mgr.project_root,
            mgr.get_active_model,
            plan_mode=plan_mode,
            get_session_stats=self.stats,
            on_plan_mode_toggle=self.toggle_plan,
            additional_roots=lambda: getattr(mgr, "additional_dirs", []),
        )
        self.prompt.on_interrupt = self.stop
        self.prompt.preview = self.preview
        self.prompt.controller_owned = True
        self.prompt.accessible = lambda: self.preferences.accessible
        self.presentation = ShellPresentation(console, self.preferences, self.invalidate)
        self.prompt.external_editor_context = self.presentation.hold
        self.active: asyncio.Task | None = None
        self.submission: PromptSubmission | None = None
        self.input_task: asyncio.Task | None = None
        self.session_views: dict[str, ShellViewState] = {}
        self.snapshots: dict[str, Any] = {}
        self.queue_paused = False
        from coderai.ui.shell.storage import ShellStorage

        self.storage = ShellStorage(mgr.project_root)
        self.recovery_error: str | None = None
        self.positions: dict[tuple[str | None, str], BrowserPosition] = {}
        self.file_preview: list[str] = []
        self.file_probe: asyncio.Task | None = None
        self.event_side: Any = None
        self.event_task: asyncio.Task | None = None
        self.events: set[asyncio.Task] = set()
        if prompt is None:
            self.prompt.session.default_buffer.on_text_changed.add_handler(self.draft_changed)

    @property
    def queue_paused(self) -> bool:
        return self.view.queue_paused

    @queue_paused.setter
    def queue_paused(self, value: bool) -> None:
        self.view.queue_paused = value

    def persist(
        self,
        *,
        draft: str | None = None,
        view: ShellViewState | None = None,
        revision: int | None = None,
    ) -> None:
        try:
            self.storage.save_view(view or self.view, draft=draft, revision=revision)
        except (OSError, ValueError, TypeError) as exc:
            message = f"Draft recovery could not be saved: {exc}"
            if message != self.recovery_error:
                self.presentation.emit(message, "warning")
            self.recovery_error = message

    def recover(self, sid: str | None) -> ShellViewState:
        try:
            values = self.storage.load_view(sid)
            if values:
                view = ShellViewState(**values)
                if view.draft or view.queue or view.attachments:
                    self.presentation.emit(
                        "Recovered draft and attachments. Queued work stays paused; /queue run resumes it."
                    )
                return view
        except (OSError, ValueError, TypeError) as exc:
            self.presentation.emit(f"Saved draft could not be recovered: {exc}", "warning")
        return ShellViewState(session_id=sid)

    @property
    def running(self) -> bool:
        return self.active is not None

    @property
    def pending_skills(self) -> list[str]:
        return self.view.pending_skills

    @pending_skills.setter
    def pending_skills(self, value: list[str]) -> None:
        self.view.pending_skills = value

    def invalidate(self) -> None:
        self.prompt.session.app.invalidate()

    def observe_session(self, sid: str | None) -> None:
        if self.event_side:
            self.event_side.close()
            self.event_side = None
        if self.event_task:
            self.event_task.cancel()
            self.event_task = None
        if not sid or not hasattr(self.mgr, "get_event_emitter"):
            return
        from coderai.wire.emitter import WireEmitter

        emitter = self.mgr.get_event_emitter(sid)
        if not isinstance(emitter, WireEmitter):
            return
        side = emitter.ui_side(merge=False, replay=False)
        self.event_side = side

        async def observe():
            from coderai.utils.aioqueue import QueueShutDown
            from coderai.wire.types import StepRetry, CompactionBegin, CompactionEnd

            try:
                while True:
                    event = await side.receive()
                    if self.view.session_id != sid:
                        continue
                    if isinstance(event, StepRetry):
                        self.presentation.pending = self.presentation.thinking = (
                            self.presentation.committed
                        ) = ""
                        self.presentation.emit(
                            f"Retry {event.next_attempt}/{event.max_attempts} in {event.wait_s:.1f}s: {event.error_type}",
                            "warning",
                        )
                    elif isinstance(event, CompactionBegin):
                        self.presentation.emit("Compacting context...", "muted")
                    elif isinstance(event, CompactionEnd):
                        self.presentation.emit(
                            "Context compaction complete. /history shows the boundary.", "muted"
                        )
            except QueueShutDown:
                return
            finally:
                side.close()

        self.event_task = asyncio.create_task(observe(), name=f"shell-events-{sid}")
        self.events.add(self.event_task)
        self.event_task.add_done_callback(self.event_finished)

    def event_finished(self, task: asyncio.Task) -> None:
        self.events.discard(task)
        if not task.cancelled() and task.exception():
            from coderai.utils.logging import logger

            logger.error("Shell event subscription failed", exc_info=task.exception())

    def draft_changed(self, buffer: Any) -> None:
        if self.file_probe:
            self.file_probe.cancel()
        if self.prompt.modal or self.input_task is None:
            return
        import copy
        import time

        snapshot = copy.deepcopy(self.view)
        revision = time.time_ns()
        text = buffer.text
        cursor = buffer.cursor_position

        async def probe():
            await asyncio.sleep(0.15)

            from coderai.ui.shell.attachments import preview_files

            snapshot.cursor = cursor
            await asyncio.to_thread(self.persist, draft=text, view=snapshot, revision=revision)
            preview = await asyncio.to_thread(preview_files, text, self.mgr.project_root)
            if self.view.session_id == snapshot.session_id:
                self.file_preview = preview
                self.invalidate()

        self.file_probe = asyncio.create_task(probe(), name="shell-file-preview")

    def preview(self):
        if self.preferences.accessible:
            return []
        size = self.prompt.session.app.output.get_size()
        result = self.presentation.preview(size.rows, size.columns)
        from coderai.ui.shell.prompt import _truncate_left

        for i, file in enumerate(self.file_preview[:3], start=len(self.view.attachments) + 1):
            label = f"{i}: {file}"
            result.append(("class:toolbar", _truncate_left(label, max(1, size.columns - 2)) + "\n"))
        if self.view.attachments:
            for i, attachment in enumerate(self.view.attachments[:3]):
                name = str(attachment.get("file_path", attachment.get("name", "image")))
                metadata = f"{attachment.get('bytes', 0):,} bytes"
                name = _truncate_left(name, max(1, size.columns - len(metadata) - 6))
                result.append(("class:toolbar", f"{i + 1}: {name} | {metadata}\n"))
            result.append(
                (
                    "class:toolbar",
                    f"Images: {len(self.view.attachments)} | /attachments remove <n>\n",
                )
            )
        if self.file_preview or self.view.attachments:
            result.append(("class:toolbar", "Ctrl-T reviews/removes attachments\n"))
        return result

    def stats(self) -> dict:
        entry = self.mgr.get_session(self.view.session_id) if self.view.session_id else None
        plan_mode = entry.plan_mode if entry and self.running else self.view.plan_mode
        pending = (
            (("paused:" if self.queue_paused else "queued:") + str(len(self.view.queue)))
            if self.view.queue
            else ""
        )
        if self.running and plan_mode != self.view.plan_mode:
            pending += f" next plan:{'ON' if self.view.plan_mode else 'off'}"
        if self.running:
            mode = "shell" if self.submission and self.submission.action == "shell" else "agent"
        else:
            mode = "shell" if self.prompt.shell_mode else "agent"
        return {
            **self.snapshots,
            "state": self.view.phase.value,
            "plan_mode": plan_mode,
            "execution_mode": mode,
            "pending": pending.strip(),
            "yolo": self.mgr.is_auto_approve(),
            "afk": self.mgr.is_afk(),
        }

    def toggle_plan(self, mode: bool) -> None:
        self.view.plan_mode = mode
        if self.view.session_id and not self.running:
            self.mgr.set_plan_mode(self.view.session_id, mode)
        elif self.running:
            self.presentation.emit(f"Plan mode {'on' if mode else 'off'} applies to the next turn.")

    def switch(self, session_id: str | None) -> None:
        if self.running:
            raise ValueError("Stop the current turn before switching sessions.")
        if self.view.session_id:
            self.session_views[self.view.session_id] = self.view
            self.persist()
        self.view = (
            self.session_views.get(session_id) or self.recover(session_id)
            if session_id
            else ShellViewState()
        )
        if session_id:
            entry = self.mgr.get_session(session_id)
            if entry:
                self.view.plan_mode = entry.plan_mode
                self.presentation.emit(
                    f"Restored {entry.summary} | {entry.status} | updated {entry.update_time} | fork: {entry.fork_of or 'none'}"
                )
                if entry.assistant_reply:
                    self.presentation.emit(entry.assistant_reply[:240])
                self.presentation.emit("/history opens the complete transcript.")
        self.prompt.update_plan_mode(self.view.plan_mode)
        self.presentation.switch(session_id)
        self.observe_session(session_id)
        self.file_preview.clear()
        self.snapshots.clear()

    def stop(self) -> bool:
        if not self.active or self.active.done():
            return False
        self.view.phase = ShellState.CANCELLING
        self.queue_paused = True
        if self.view.session_id:
            self.mgr.interrupt_session(self.view.session_id)
        self.active.cancel()
        return True

    def prepare(
        self, text: str, action: Literal["send", "queue", "steer", "shell"] = "send"
    ) -> PromptSubmission:
        submission = prepare_submission(
            text,
            self.mgr.project_root,
            skills=self.pending_skills,
            attachments=self.view.attachments,
        )
        submission.action = action
        if submission.attachments:
            from coderai.utils.common.model_capabilities import supports_multimodal

            settings = self.mgr.get_resolved_settings()
            if not supports_multimodal(
                self.mgr.get_active_model(), settings.get("multimodal", "default")
            ):
                raise ValueError(
                    "The active model does not support images. Select an image-capable model before submitting."
                )
        self.view.attachments = []
        self.pending_skills = []
        return submission

    async def prepare_async(
        self, text: str, action: Literal["send", "queue", "steer", "shell"] = "send"
    ) -> PromptSubmission | None:
        from coderai.ui.shell.prompt import (
            AmbiguousFileMention,
            FILE_MENTION_PATTERN,
            _parse_line_range,
        )

        try:
            return await asyncio.to_thread(self.prepare, text, action)
        except AmbiguousFileMention as exc:
            selected = await self.select(
                f"Choose file: {exc.reference}",
                [
                    BrowserRow(str(path), str(path), f"{path.stat().st_size:,} bytes")
                    for path in exc.paths
                ],
            )
            if selected.cancelled:
                self.view.draft = text
                self.view.cursor = len(text)
                return None
            reference = exc.reference

            def replace(match):
                path, start, end = _parse_line_range(match[1])
                if path != reference:
                    return match[0]
                line_range = f":{start}-{end}" if start else ""
                return f'@"{selected.value}"{line_range}'

            return await self.prepare_async(FILE_MENTION_PATTERN.sub(replace, text), action)

    def start(self, submission: PromptSubmission) -> None:
        if self.running:
            raise RuntimeError("A turn already owns execution")
        self.submission = submission
        self.view.phase = ShellState.RUNNING
        self.presentation.start_turn()
        new = self.view.session_id is None or self.mgr.get_session(self.view.session_id) is None
        if self.view.session_id is None:
            self.view.session_id = uuid.uuid4().hex
            self.presentation.switch(self.view.session_id)
        sid = self.view.session_id
        self.observe_session(sid)

        async def execute():
            if submission.action == "command":
                from coderai.ui.shell.controller_commands import execute_foreground_command

                await execute_foreground_command(self, submission.resolved_text)
                return
            if submission.action == "shell":
                await self.execute_shell(submission)
                return
            if new:
                await self.mgr.create_session(
                    submission.resolved_text,
                    plan_mode=self.view.plan_mode,
                    skills=submission.skills or None,
                    content_params=submission.attachments or None,
                    session_id=sid,
                )
            else:
                await self.mgr.reply_session(
                    sid,
                    submission.resolved_text or None,
                    plan_mode=self.view.plan_mode,
                    skills=submission.skills or None,
                    content_params=submission.attachments or None,
                )

        self.view.last_failed = submission
        if new:
            self.persist()
            try:
                self.storage.delete_view(None)
            except (OSError, ValueError):
                self.presentation.emit(
                    "The old draft recovery record could not be cleared.", "warning"
                )
        self.active = asyncio.create_task(execute(), name=f"shell-turn-{sid}")

    async def execute_shell(self, submission: PromptSubmission) -> None:
        import os
        import tempfile
        from coderai.share import get_share_dir
        from coderai.utils.file_tail import tail_file
        from coderai.utils.subprocess_env import kill_process_tree

        if self.mgr.get_session(self.view.session_id) is None:
            self.view.session_id = await self.mgr.create_empty_session(
                plan_mode=self.view.plan_mode
            )
            self.presentation.switch(self.view.session_id)
            self.observe_session(self.view.session_id)
        sid = self.view.session_id
        folder = get_share_dir() / "job-output"
        folder.mkdir(parents=True, exist_ok=True)
        job_id = "shell-" + uuid.uuid4().hex[:12]
        with tempfile.NamedTemporaryFile(
            dir=folder, prefix=job_id, suffix=".log", delete=False
        ) as output:
            proc = await asyncio.create_subprocess_shell(
                submission.resolved_text,
                cwd=self.mgr.project_root,
                stdout=output,
                stderr=output,
                start_new_session=os.name != "nt",
            )
            try:
                self.mgr.job_store.start(
                    job_id=job_id,
                    session_id=sid,
                    kind="shell",
                    label=submission.resolved_text,
                    process_id=proc.pid,
                    output_path=output.name,
                )
                await asyncio.wait_for(proc.wait(), timeout=120)
            except BaseException:
                await asyncio.to_thread(kill_process_tree, proc.pid)
                await proc.wait()
                self.mgr.job_store.complete(
                    job_id, ok=False, exit_code=proc.returncode, detail="interrupted or failed"
                )
                raise
            self.mgr.job_store.complete(job_id, ok=proc.returncode == 0, exit_code=proc.returncode)
            self.presentation.emit(await asyncio.to_thread(tail_file, output.name, lines=40))
            self.presentation.emit(
                f"Shell exit {proc.returncode}. /activity opens complete output for {job_id}."
            )

    async def suspend_input(self) -> None:
        if self.input_task:
            self.view.draft = self.prompt.session.default_buffer.text
            self.view.cursor = self.prompt.session.default_buffer.cursor_position
            self.input_task.cancel()
            await asyncio.gather(self.input_task, return_exceptions=True)
            self.input_task = None
        if self.file_probe:
            self.file_probe.cancel()
            await asyncio.gather(self.file_probe, return_exceptions=True)
            self.file_probe = None

    async def select(self, title: str, rows: Any):
        self.prompt.modal = True
        try:
            key = (self.view.session_id, title)
            position = self.positions.setdefault(key, BrowserPosition())
            with self.presentation.hold():
                return await choose(
                    self.prompt,
                    title,
                    rows,
                    accessible=self.preferences.accessible,
                    position=position,
                )
        finally:
            self.prompt.modal = False

    async def ask(self, message: str) -> str | None:
        self.prompt.modal = True
        try:
            return await self.prompt.prompt_async(message)
        except (KeyboardInterrupt, EOFError):
            return None
        finally:
            self.prompt.modal = False

    async def questions(self, questions: list[dict]) -> QuestionOutcome:
        answers = []
        for question in questions:
            answer: str | None = None
            label = str(question.get("question", question.get("header", "Question")))
            options = question.get("options") or []
            if options:
                values = [
                    str(o.get("label", o.get("title", ""))) if isinstance(o, dict) else str(o)
                    for o in options
                ]
                chosen: list[str] = []
                multiple = question.get("multiSelect", question.get("multi_select", False))
                while True:
                    rows = [
                        BrowserRow(
                            str(i),
                            (f"[{'x' if value in chosen else ' '}] {value}" if multiple else value),
                            str(options[i].get("description", ""))
                            if isinstance(options[i], dict)
                            else "",
                        )
                        for i, value in enumerate(values)
                    ]
                    rows += [BrowserRow("other", "Other: enter text")]
                    if multiple:
                        rows += [
                            BrowserRow(
                                "done",
                                f"Submit {len(chosen)} selected answers",
                                ", ".join(chosen) or "Select at least one answer.",
                                disabled=not chosen,
                            )
                        ]
                    result = await self.select(
                        label + (" | Enter toggles; choose Submit to finish" if multiple else ""),
                        rows,
                    )
                    if result.cancelled:
                        return QuestionOutcome("cancelled")
                    if result.value == "done" and chosen:
                        answer = ", ".join(chosen)
                        break
                    answer = (
                        await self.ask("Answer: ")
                        if result.value == "other"
                        else values[int(result.value)]
                    )
                    if not answer:
                        return QuestionOutcome("incomplete")
                    if not multiple:
                        break
                    if answer in chosen:
                        chosen.remove(answer)
                    else:
                        chosen.append(answer)
            else:
                answer = await self.ask(f"{label}: ")
            if not answer or not answer.strip():
                return QuestionOutcome("cancelled" if answer is None else "incomplete")
            answers.append((label, answer))
        return QuestionOutcome("submitted", answers)

    async def drain(self) -> None:
        sid = self.view.session_id
        while sid:
            entry = self.mgr.get_session(sid)
            if not entry:
                return
            if entry.status == "ask_permission":
                self.view.phase = ShellState.AWAITING_APPROVAL
                replies = []
                for request in entry.ask_permissions or []:
                    if self.yes or self.mgr.is_auto_approve():
                        decision = "once"
                    else:
                        from coderai.ui.shell.security import redact_settings

                        details = json.dumps(redact_settings(request), ensure_ascii=False, indent=2)
                        rows = [
                            BrowserRow("once", "Allow once", details),
                            BrowserRow("deny", "Deny", details),
                            BrowserRow("feedback", "Deny with feedback", details),
                        ]
                        if not entry.plan_mode:
                            rows.insert(1, BrowserRow("session", "Allow for this session", details))
                        selected = await self.select(
                            "Approval: " + str(request.get("name", "action")), rows
                        )
                        if selected.cancelled:
                            self.mgr.interrupt_session(sid)
                            self.queue_paused = True
                            return
                        decision = selected.value
                    reply = {
                        "toolCallId": request.get("toolCallId"),
                        "permission": "allow" if decision in ("once", "session") else "deny",
                    }
                    if decision == "session":
                        reply["decision"] = "approve_for_session"
                    if decision == "feedback":
                        feedback = await self.ask("Feedback: ")
                        if feedback is None:
                            self.mgr.interrupt_session(sid)
                            return
                        reply["feedback"] = feedback
                    replies.append(reply)
                self.view.phase = ShellState.RUNNING
                self.active = asyncio.create_task(
                    self.mgr.reply_session(sid, permission_replies=replies)
                )
                return
            elif entry.status in ("ask_user_question", "waiting_for_user"):
                self.view.phase = ShellState.AWAITING_ANSWER
                messages = self.mgr.list_session_messages(sid)
                latest = next(
                    (m for m in reversed(messages) if m.role == "tool" and not m.compacted), None
                )
                try:
                    payload = json.loads(latest.content) if latest else {}
                    questions = (
                        (payload.get("metadata") or {}).get("questions")
                        or entry.ask_permissions
                        or []
                    )
                except (ValueError, AttributeError):
                    questions = entry.ask_permissions or []
                if not questions:
                    self.queue_paused = True
                    return
                outcome = await self.questions(questions)
                if outcome.status != "submitted":
                    self.mgr.interrupt_session(sid)
                    self.queue_paused = True
                    return
                self.view.phase = ShellState.RUNNING
                self.active = asyncio.create_task(self.mgr.reply_session(sid, outcome.text))
                return
            else:
                return

    async def settle(self) -> None:
        await self.suspend_input()
        task, self.active = self.active, None
        if task is None:
            return
        try:
            await task
            await self.drain()
            if self.active:
                return
            entry = self.mgr.get_session(self.view.session_id)
            failed = bool(entry and entry.status == "failed")
            self.view.phase = ShellState.FAILED if failed else ShellState.IDLE
            outcome = entry.status if entry else "complete"
            if not failed and outcome in ("ready", "complete", "completed"):
                self.view.last_failed = None
            if failed or (entry and entry.status == "interrupted"):
                self.view.last_failed = self.submission
                self.queue_paused = True
                self.presentation.emit(
                    "/retry resubmits with attachments using the current workspace; partial changes remain."
                )
        except asyncio.CancelledError:
            self.view.phase = ShellState.IDLE
            self.view.last_failed = self.submission
            outcome = "interrupted; queue paused"
        except Exception as exc:
            self.view.phase = ShellState.FAILED
            self.view.last_failed = self.submission
            self.queue_paused = True
            outcome = "failed"
            self.presentation.emit(
                f"Turn failed: {exc}. /retry uses the current workspace and keeps partial changes.",
                "red",
            )
        self.presentation.complete(outcome)
        await asyncio.to_thread(self.persist)

    async def inspect(self, title: str, content: str) -> None:
        with self.presentation.hold():
            await inspect_output(
                self.prompt, title, content, accessible=self.preferences.accessible
            )

    async def handle(self, raw: str, *, steer: bool = False) -> bool:
        from coderai.ui.shell.controller_commands import dispatch_controller_command

        try:
            result = await dispatch_controller_command(self, raw, steer=steer)
        except (ValueError, OSError):
            self.view.draft = raw
            self.view.cursor = len(raw)
            raise
        finally:
            await asyncio.to_thread(self.persist)
        return result

    async def tasks(self) -> None:
        from coderai.ui.shell.controller_commands import browse_activity

        await browse_activity(self)

    async def undo(self) -> None:
        from coderai.ui.shell.browsers import restore_checkpoint

        await restore_checkpoint(self)

    async def apply_plan(self) -> None:
        from coderai.ui.shell.controller_commands import review_plan

        await review_plan(self)

    async def refresh(self) -> None:
        from coderai.ui.shell.prompt import get_git_status
        from coderai.ui.shell.runtime_view import context_limit

        while True:
            await asyncio.to_thread(get_git_status, self.mgr.project_root)
            entry = self.mgr.get_session(self.view.session_id) if self.view.session_id else None
            settings = self.mgr.get_resolved_settings()
            permission = (settings.get("permissions") or {}).get("sandbox", "ask")
            permission = {
                "danger-full-access": "full",
                "workspace-write": "workspace",
                "read-only": "read",
            }.get(permission, permission)
            self.snapshots = {
                "tokens": entry.active_tokens if entry else 0,
                "context_limit": context_limit(self.mgr),
                "permission": permission,
                "plan_mode": entry.plan_mode if entry and self.running else self.view.plan_mode,
                "agent_role": self.mgr.get_active_agent_role(),
                "reasoning_effort": self.mgr.get_reasoning_effort(),
            }
            self.invalidate()
            await asyncio.sleep(2)

    async def run(self, *, initial_prompt: str | None = None) -> int:
        from coderai.utils.term import ensure_new_line, ensure_tty_sane
        from coderai.ui.shell.welcome import render_welcome_screen

        try:
            ensure_tty_sane()
            ensure_new_line()
        except Exception:
            pass

        # Render Welcome Screen & Brand Identity
        try:
            mcp_count = len(getattr(self.mgr.mcp_manager, "clients", {}) or {})
        except Exception:
            mcp_count = 0
        try:
            from coderai.skill import list_skills

            discovered_skills = list_skills(self.mgr.project_root)
        except Exception:
            discovered_skills = []
        render_welcome_screen(
            self.console,
            self.mgr.project_root,
            self.mgr.get_active_model(),
            plan_mode=self.view.plan_mode,
            mcp_servers_count=mcp_count,
            skills_count=len(discovered_skills),
            reasoning_effort=self.mgr.get_reasoning_effort()
            if hasattr(self.mgr, "get_reasoning_effort")
            else "max",
            active_agent=self.mgr.get_active_agent_role()
            if hasattr(self.mgr, "get_active_agent_role")
            else "default",
        )

        previous = (
            self.mgr._assistant_callback,
            self.mgr.on_stream_chunk,
            self.mgr.on_thinking_chunk,
        )
        self.mgr._assistant_callback = self.presentation.message
        self.mgr.on_stream_chunk = self.presentation.chunk
        self.mgr.on_thinking_chunk = self.presentation.thinking_chunk
        refresh = asyncio.create_task(self.refresh(), name="shell-status")
        # Keep one input owner between accepted prompts. Nested PTK prompts
        # restore this raw state, preventing fast Enter from becoming Ctrl-J
        # during the gap between input tasks. Restore the original state once.
        raw_mode = self.prompt.session.app.input.raw_mode()
        raw_mode.__enter__()
        try:
            if self.view.session_id:
                await self.drain()
            if initial_prompt:
                prepared = self.prepare(initial_prompt)
                if self.running:
                    self.view.queue.append(prepared)
                else:
                    self.start(prepared)
            while self.view.phase != ShellState.CLOSED:
                if not self.running and self.view.queue and not self.queue_paused:
                    self.start(self.view.queue.pop(0))
                if not self.input_task:
                    self.input_task = asyncio.create_task(
                        self.prompt.prompt_async(default=self.view.draft, cursor=self.view.cursor)
                    )
                    self.view.draft = ""
                    self.view.cursor = None
                tasks = {self.input_task}
                if self.active:
                    tasks.add(self.active)
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                if self.input_task in done:
                    task, self.input_task = self.input_task, None
                    try:
                        raw = task.result()
                        if self.prompt.attachments_requested:
                            self.prompt.attachments_requested = False
                            self.view.draft = raw
                            await self.handle("/attachments edit")
                        elif self.prompt.inspect_requested:
                            self.prompt.inspect_requested = False
                            self.view.draft = raw
                            await self.handle("/output")
                        elif not await self.handle(raw, steer=self.prompt.pop_steer()):
                            break
                    except EOFError:
                        self.stop()
                        break
                    except KeyboardInterrupt:
                        self.stop()
                    except (ValueError, OSError) as exc:
                        self.presentation.emit(str(exc), "red")
                    except Exception as exc:
                        from coderai.utils.logging import logger

                        logger.exception("Terminal command failed")
                        self.presentation.emit(f"Command failed: {exc}", "red")
                if self.active and self.active.done():
                    await self.settle()
            return 0
        finally:
            await self.suspend_input()
            await asyncio.to_thread(self.persist)
            self.stop()
            if self.active:
                await asyncio.gather(self.active, return_exceptions=True)
                self.active = None
            refresh.cancel()
            await asyncio.gather(refresh, return_exceptions=True)
            if self.file_probe:
                self.file_probe.cancel()
                await asyncio.gather(self.file_probe, return_exceptions=True)
            self.observe_session(None)
            if self.events:
                await asyncio.gather(*self.events, return_exceptions=True)
            self.mgr._assistant_callback, self.mgr.on_stream_chunk, self.mgr.on_thinking_chunk = (
                previous
            )
            self.view.phase = ShellState.CLOSED
            raw_mode.__exit__(None, None, None)
            from coderai.cli.exit_summary import render_exit_summary

            render_exit_summary(self.console, self.mgr, self.view.session_id)


async def run_shell(
    mgr: Any,
    console: Any,
    yes: bool,
    resume: str | bool | None,
    fork: str | bool | None,
    last: bool,
    plan_mode: bool,
    initial_prompt: str | None,
) -> int:
    controller = ShellController(mgr, console, yes=yes, plan_mode=plan_mode)
    sid = None
    entries = mgr.list_sessions() if last or fork or resume is True else []
    if last and entries:
        sid = entries[0].id
    elif fork:
        source = (
            mgr.resolve_session_id(fork)
            if isinstance(fork, str)
            else entries[0].id
            if entries
            else None
        )
        sid = mgr.fork_session(source) if source else None
        if not sid:
            controller.presentation.emit("No session found to fork.")
            return 1
    elif isinstance(resume, str):
        sid = mgr.resolve_session_id(resume)
        if not sid:
            controller.presentation.emit(f"Session not found: {resume}")
            return 1
    elif resume is True:
        chosen = await controller.select(
            "Resume session",
            [BrowserRow(e.id, e.summary, e.assistant_reply or "") for e in entries],
        )
        sid = chosen.value
    if not any((last, fork, resume)):
        try:
            saved = controller.storage.last_session()
            recovered = controller.storage.load_view(saved)
            if (
                saved
                and mgr.get_session(saved)
                and any(
                    recovered.get(key) for key in ("draft", "attachments", "queue", "last_failed")
                )
            ):
                sid = saved
            else:
                controller.view = controller.recover(None)
        except (OSError, ValueError) as exc:
            controller.presentation.emit(f"Recovery unavailable: {exc}", "warning")
    if sid:
        controller.switch(str(sid))
    controller.view.plan_mode = controller.view.plan_mode or plan_mode
    controller.prompt.update_plan_mode(controller.view.plan_mode)
    return await controller.run(initial_prompt=initial_prompt)
