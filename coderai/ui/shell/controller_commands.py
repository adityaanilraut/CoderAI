"""Command routing and focused browsers for the async shell controller."""

from __future__ import annotations
import asyncio
import json
from pathlib import Path
from typing import Any

from coderai.ui.shell.interaction import BrowserRow
from coderai.ui.shell.submission import PromptSubmission, prepare_submission, split_path_argument


async def _pick_model(self: Any, grouped: dict[str, list[Any]], provider: str | None) -> str | None:
    from coderai.ui.shell.interaction import BrowserPosition
    from coderai.ui.shell.model_menu import ModelMenu
    from coderai.ui.shell.session_picker import _PROVIDER_LABELS

    from coderai.ui.shell.model_details import describe_models

    grouped = await asyncio.to_thread(
        describe_models,
        grouped,
        self.mgr.project_root,
        self.mgr.get_resolved_settings(),
        self.mgr.get_active_model(),
    )
    library = self.storage.library()
    all_models = dict(item for models in grouped.values() for item in models)
    favorites = [
        (m, all_models[m])
        for m, enabled in library.get("favorite_models", {}).items()
        if enabled and m in all_models
    ]
    recent = [
        (m, all_models[m])
        for m in sorted(
            library.get("recent_models", {}), key=library.get("recent_models", {}).get, reverse=True
        )[:10]
        if m in all_models
    ]
    grouped = {
        **({"favorites": favorites} if favorites else {}),
        **({"recent": recent} if recent else {}),
        **grouped,
    }
    labels = {
        **_PROVIDER_LABELS,
        "favorites": ("Favorites", "Saved models"),
        "recent": ("Recent", "Recently used models"),
    }
    if provider in ("favorites", "recent") and not grouped.get(provider):
        self.presentation.emit(f"No {provider} models yet. /model favorite saves the active model.")
        return None
    menu = ModelMenu(grouped, labels, self.mgr.get_active_model(), provider)
    from coderai.openrouter import is_openrouter_model, openrouter_model_available

    if (
        is_openrouter_model(menu.current_model)
        and openrouter_model_available(menu.current_model) is False
        and not any(
            name == menu.current_model for models in menu.grouped.values() for name, _ in models
        )
    ):
        self.presentation.emit(
            f"Active model {menu.current_model} is absent from the latest OpenRouter catalog. "
            "Select an available model; refresh alone does not switch the active model."
        )
        if provider is None and "openrouter" in menu.grouped:
            menu.provider = "openrouter"
    while True:
        self.positions.setdefault(
            (self.view.session_id, menu.title), BrowserPosition(selected_id=menu.default_id)
        )
        selected = await self.select(menu.title, menu.rows())
        if menu.advance(None if selected.cancelled else str(selected.value)):
            return menu.selected_model


async def dispatch_controller_command(self: Any, raw: str, *, steer: bool = False) -> bool:
    from coderai.ui.shell.slash import parse_slash_command
    from coderai.ui.shell.dispatch import ShellContext, SlashAction, dispatch_slash_command

    if not raw.strip():
        return True
    if not raw.startswith("/"):
        shell = raw.startswith("!") or getattr(self.prompt, "shell_mode", False)
        submission = (
            PromptSubmission(raw, raw[1:].strip() if raw.startswith("!") else raw, action="shell")
            if shell
            else await self.prepare_async(
                raw, "steer" if steer and self.running else "queue" if self.running else "send"
            )
        )
        if submission is None:
            return True
        if submission.attachments:
            from coderai.ui.shell.submission_history import SubmissionHistory

            label = await asyncio.to_thread(
                SubmissionHistory(self.mgr.project_root).save,
                submission.display_text,
                submission.attachments,
            )
            await asyncio.to_thread(self.prompt.remember_submission, label)
        if steer and self.running:
            if self.submission and self.submission.action in ("shell", "command"):
                raise ValueError(
                    "This command cannot accept steering. Use /stop or queue a follow-up."
                )
            if submission.attachments:
                self.view.attachments.extend(submission.attachments)
                raise ValueError("Steering accepts text. Images remain attached for the next turn.")
            self.mgr.steer_session(self.view.session_id, submission.resolved_text)
            self.presentation.emit("Steering accepted for the current turn.")
        elif self.running:
            self.view.queue.append(submission)
            self.presentation.emit(
                f"Queued #{len(self.view.queue)}: {submission.display_text[:100]}"
            )
        else:
            self.start(submission)
        return True
    cmd, args = parse_slash_command(raw)
    if cmd == "/exit":
        self.stop()
        return False
    if cmd == "/stop":
        self.presentation.emit(
            "Cancellation requested; queue paused." if self.stop() else "No turn is running."
        )
    elif cmd == "/steer":
        if not args:
            self.presentation.emit("Usage: /steer <text>")
        else:
            await self.handle(args, steer=True)
    elif cmd == "/queue":
        parts = args.split()
        operation = parts[0] if parts else "list"
        queue = self.view.queue
        if operation == "clear":
            queue.clear()
        elif operation == "edit":
            if len(parts) != 2 or not parts[1].isdigit() or not 1 <= int(parts[1]) <= len(queue):
                raise ValueError("Usage: /queue edit <n>")
            if self.view.draft or self.view.attachments:
                raise ValueError(
                    "Finish or clear the current draft before editing a queued prompt."
                )
            item = queue.pop(int(parts[1]) - 1)
            self.view.draft = (
                "!" + item.resolved_text if item.action == "shell" else item.display_text
            )
            self.view.cursor = len(self.view.draft)
            self.view.attachments = item.attachments
            self.pending_skills = item.skills
            self.queue_paused = True
            self.presentation.emit("Queued prompt returned to composer; remaining queue paused.")
        elif operation in ("remove", "move"):
            expected = 2 if operation == "remove" else 3
            if len(parts) != expected or not all(p.isdigit() for p in parts[1:]):
                raise ValueError("Usage: /queue remove <n> or /queue move <from> <to>")
            indices = [int(p) - 1 for p in parts[1:]]
            if any(i < 0 or i >= len(queue) for i in indices):
                raise ValueError("Queue index is out of range.")
            value = queue.pop(indices[0])
            if operation == "move":
                queue.insert(indices[1], value)
        elif operation == "run":
            self.queue_paused = False
        elif operation != "list":
            raise ValueError("Usage: /queue [list|edit <n>|remove|move|clear|run]")
        self.presentation.emit(
            "Queue paused; /queue run resumes." if self.queue_paused else "Queue active."
        )
        for i, item in enumerate(queue):
            self.presentation.emit(f"{i + 1}. {item.display_text} ({len(item.attachments)} images)")
        if not queue:
            self.presentation.emit("Queue empty.")
    elif cmd == "/retry":
        if self.running:
            raise ValueError("Stop the current turn before retrying.")
        if self.view.last_failed:
            self.presentation.emit(
                "Retry uses the current workspace; partial changes are retained."
            )
            self.start(self.view.last_failed)
        else:
            self.presentation.emit("No failed submission to retry.")
    elif cmd == "/editor":
        from coderai.utils.editor import open_external_editor
        from coderai.ui.shell.placeholders import get_placeholder_manager

        manager = get_placeholder_manager()
        original = self.view.draft or args
        with self.presentation.hold(), self.prompt.session.app.input.cooked_mode():
            edited = await asyncio.to_thread(
                open_external_editor, manager.expand_for_editor(original)
            )
        if edited is not None:
            self.view.draft = manager.refold_after_editor(edited, original)
    elif cmd == "/paste":
        text = await self.ask("Paste text (Ctrl-J newline; Enter returns it to the composer): ")
        if text is not None:
            self.view.draft = text
    elif cmd == "/model":
        if self.running:
            raise ValueError("Stop the turn before changing the model.")
        from coderai.ui.shell.session_picker import get_models_by_provider, _PROVIDER_LABELS

        operation, _, model_arg = args.partition(" ")
        if operation in ("favorite", "unfavorite"):
            model = model_arg.strip() or self.mgr.get_active_model()
            await asyncio.to_thread(
                self.storage.update_library, "favorite_models", model, operation == "favorite"
            )
            self.presentation.emit(
                f"{'Saved' if operation == 'favorite' else 'Removed'} favorite: {model}"
            )
            return True
        if operation == "verify":
            from coderai.ui.shell.setup import run_connectivity_test_interactive

            await asyncio.to_thread(
                run_connectivity_test_interactive,
                self.console,
                self.mgr.project_root,
                self.mgr.get_active_model(),
            )
            return True
        if args.lower() == "refresh":
            from coderai.openrouter import refresh_openrouter_catalog

            _, status = await asyncio.to_thread(refresh_openrouter_catalog)
            self.presentation.emit(status)
            args = ""
        grouped = await asyncio.to_thread(
            get_models_by_provider, self.mgr.get_active_model(), refresh_openrouter=False
        )
        provider = (
            next(
                (
                    key
                    for key in grouped
                    if args.lower()
                    in (key.lower(), _PROVIDER_LABELS.get(key, (key, ""))[0].lower())
                ),
                None,
            )
            if args
            else None
        )
        if args in ("favorites", "recent"):
            provider = args
        if args and provider is None:
            self.mgr.set_model(args)
            await asyncio.to_thread(self.storage.remember_model, self.mgr.get_active_model())
            self.presentation.emit(f"Active model: {self.mgr.get_active_model()}")
            return True
        selected_model = await _pick_model(self, grouped, provider)
        if selected_model is not None:
            self.mgr.set_model(selected_model)
            await asyncio.to_thread(self.storage.remember_model, self.mgr.get_active_model())
            self.presentation.emit(f"Active model: {self.mgr.get_active_model()}")
    elif cmd == "/agent" and args.lower() in ("", "switch"):
        if self.running:
            raise ValueError("Stop the turn before switching the active role.")
        from coderai.subagents.registry import discover_markdown_agents

        discovered = await asyncio.to_thread(discover_markdown_agents, Path(self.mgr.project_root))
        rows = [BrowserRow("default", "default"), BrowserRow("okabe", "okabe")]
        rows.extend(
            BrowserRow(role.name, role.name, f"{role.mode}: {role.description}")
            for role in discovered
        )
        selected = await self.select("Agent role", rows)
        if not selected.cancelled:
            if not self.mgr.switch_agent_role(str(selected.value), session_id=self.view.session_id):
                raise ValueError(f"Unable to switch to role: {selected.value}")
            self.presentation.emit(f"Active role: {selected.value}")
    elif cmd in ("/effort", "/permission") and not args:
        if self.running:
            raise ValueError("Stop the turn before changing execution settings.")
        if cmd == "/effort":
            from coderai.utils.common.model_capabilities import get_supported_reasoning_efforts

            current = self.mgr.get_reasoning_effort()
            rows = [
                BrowserRow(value, value + (" (current)" if value == current else ""))
                for value in get_supported_reasoning_efforts(self.mgr.get_active_model())
            ]
        else:
            from coderai.sandbox import SANDBOX_MODES, preset_permissions

            current = (self.mgr.get_resolved_settings().get("permissions") or {}).get("sandbox")
            rows = []
            for value in SANDBOX_MODES:
                policy = preset_permissions(value)
                detail = "\n".join(
                    f"{label}: {', '.join(policy[label]) or 'none'}"
                    for label in ("allow", "deny", "ask")
                )
                rows.append(
                    BrowserRow(
                        value,
                        value + (" (current)" if value == current else ""),
                        detail + "\nSaved to project settings. Applies to new sessions.",
                    )
                )
        selected = await self.select(cmd[1:].title(), rows)
        if not selected.cancelled:
            await self.handle(f"{cmd} {selected.value}")
    elif cmd in ("/image", "/attach"):
        path, text = split_path_argument(args)
        if not path:
            raise ValueError("Usage: /image <quoted path> [prompt] or /attach <path>")
        from coderai.cli.image_attachment import parse_and_attach_image
        from coderai.utils.common.model_capabilities import supports_multimodal

        if cmd == "/image":
            if not supports_multimodal(
                self.mgr.get_active_model(),
                self.mgr.get_resolved_settings().get("multimodal", "default"),
            ):
                raise ValueError("Select an image-capable model before attaching an image.")
            param, error = await asyncio.to_thread(
                parse_and_attach_image, path, self.mgr.project_root
            )
            if error or param is None:
                raise ValueError(error or "Image attachment metadata is unavailable.")
            self.view.attachments.append(param)
            self.view.draft = text or f"Inspect image: {path}"
            self.presentation.emit(
                f"Attached {param['file_path']} ({param['bytes']:,} bytes). Enter submits; /attachments removes images."
            )
        else:
            reference = f'@"{path}"'
            from coderai.ui.shell.prompt import AmbiguousFileMention

            try:
                prepared = await asyncio.to_thread(
                    prepare_submission, reference, self.mgr.project_root
                )
            except AmbiguousFileMention as exc:
                selected = await self.select(
                    f"Choose file: {path}", [BrowserRow(str(p), str(p)) for p in exc.paths]
                )
                if selected.cancelled:
                    return True
                reference = f'@"{selected.value}"'
                prepared = await asyncio.to_thread(
                    prepare_submission, reference, self.mgr.project_root
                )
            self.view.draft = (self.view.draft + " " + reference).strip()
            self.presentation.emit(f"File prepared: {', '.join(prepared.files)}")
    elif cmd == "/attachments":
        from coderai.ui.shell.attachments import manage_attachments

        await manage_attachments(self, args)
    elif cmd == "/tools":
        from coderai.ui.shell.dispatch import effective_tool_rows

        ctx = ShellContext(self.mgr, self.view.session_id, self.view.plan_mode, self.console)
        records = await asyncio.to_thread(effective_tool_rows, ctx)
        rows = [
            BrowserRow(
                r["name"],
                f"{r['name']}: {'conditional' if r.get('conditional') else 'available' if r['available'] else 'restricted'}",
                json.dumps(r, indent=2),
            )
            for r in records
        ]
        if args:
            from coderai.ui.shell.interaction import BrowserPosition

            self.positions[(self.view.session_id, "Tools")] = BrowserPosition(query=args)
        while True:
            selected = await self.select("Tools", rows)
            row = next((r for r in rows if r.id == selected.value), None)
            if row is None:
                break
            await self.inspect(row.label, row.detail)
    elif cmd == "/diff":
        from coderai.ui.shell.browsers import browse_diff

        await browse_diff(self)
    elif cmd == "/mcp" and not args:
        from coderai.ui.shell.browsers import browse_integrations

        await browse_integrations(self)
    elif cmd in ("/history", "/output"):
        from coderai.ui.shell.output import message_output

        messages = (
            self.mgr.list_session_messages(self.view.session_id) if self.view.session_id else []
        )
        messages = [m for m in messages if cmd == "/history" or m.role == "tool"]
        rows = [
            BrowserRow(
                m.id,
                f"{m.role} {m.create_time} {'[compaction boundary]' if (m.meta or {}).get('isSummary') or (m.meta or {}).get('kind') == 'compact/summary' else '[compacted]' if m.compacted else ''} {(m.content or '')[:100]}",
                (m.thinking or "") + "\n" + (m.content or ""),
            )
            for m in messages
        ]
        if args:
            direct = next((m for m in messages if args in (m.id, m.tool_call_id)), None)
            if direct:
                text = await asyncio.to_thread(message_output, direct, self.mgr)
                await self.inspect(f"{direct.role}: {direct.id}", text)
                return True
            retained = self.presentation.outputs.items.get(args) if cmd == "/output" else None
            if retained:
                await self.inspect(*retained)
                return True
            from coderai.ui.shell.interaction import BrowserPosition

            title = "Transcript" if cmd == "/history" else "Tool output"
            self.positions[(self.view.session_id, title)] = BrowserPosition(query=args)
        while True:
            selected = await self.select("Transcript" if cmd == "/history" else "Tool output", rows)
            row = next((r for r in rows if r.id == selected.value), None)
            if row is None:
                break
            message = next(m for m in messages if m.id == row.id)
            text = await asyncio.to_thread(message_output, message, self.mgr)
            await self.inspect(row.label, text)
    elif cmd in ("/task", "/activity"):
        await self.tasks()
    elif cmd in ("/sessions", "/new", "/fork", "/delete", "/reset", "/undo"):
        if self.running:
            raise ValueError("Stop the current turn before changing session history.")
        if cmd == "/sessions":
            from coderai.ui.shell.browsers import browse_sessions

            await browse_sessions(self, args)
        elif cmd == "/new":
            self.switch(None)
        elif cmd == "/undo":
            await self.undo()
        elif cmd == "/delete":
            sid = self.mgr.resolve_session_id(args) if args else self.view.session_id
            if not sid:
                raise ValueError("Session not found.")
            if not args:
                selected = await self.select(
                    "Delete current session?",
                    [
                        BrowserRow("keep", "Keep session"),
                        BrowserRow("delete", "Delete session and saved history"),
                    ],
                )
                if selected.value != "delete":
                    return True
            if self.mgr.delete_session(sid):
                if sid == self.view.session_id:
                    self.switch(None)
                self.session_views.pop(sid, None)
                self.storage.delete_view(sid)
            else:
                self.presentation.emit("Session deletion failed.", "error")
        else:
            ctx = ShellContext(
                self.mgr,
                self.view.session_id,
                self.view.plan_mode,
                self.console,
                self.yes,
                self.pending_skills,
                self.prompt,
            )
            await dispatch_slash_command(cmd, args, ctx)
            self.switch(ctx.session_id)
    elif cmd == "/display":
        if args in ("compact", "verbose"):
            self.preferences.detail = "verbose" if args == "verbose" else "compact"
        elif args in ("accessible", "animated"):
            self.preferences.accessible = args == "accessible"
        elif args:
            raise ValueError("Usage: /display [compact|verbose|accessible|animated]")
        self.preferences.save(self.mgr.project_root)
        self.presentation.emit(
            f"Display: {self.preferences.detail}; accessible: {self.preferences.accessible}"
        )
    elif cmd == "/thinking":
        if args.lower() not in ("", "full", "on", "summary", "lite", "normal", "off"):
            raise ValueError("Usage: /thinking [full|summary]")
        verbose = args.lower() in ("full", "on") or (
            not args and self.preferences.detail == "compact"
        )
        self.preferences.detail = "verbose" if verbose else "compact"
        self.preferences.save(self.mgr.project_root)
        self.presentation.emit(f"Reasoning display: {'full' if verbose else 'summary'}")
    elif cmd == "/plan" and args == "apply":
        await self.apply_plan()
    elif cmd == "/continue":
        if self.running:
            raise ValueError("A turn is already running.")
        self.start(PromptSubmission("/continue", ""))
    elif (
        cmd in ("/review", "/btw", "/compact")
        or cmd.startswith("/flow:")
        or (cmd == "/goal" and args.split()[:1] == ["start"])
    ):
        if self.running:
            raise ValueError("Wait for the active turn or use /stop before starting this command.")
        self.start(PromptSubmission(raw, raw, action="command"))
    else:
        goal_control = cmd == "/goal" and (
            not args or args.split()[0] in ("list", "pause", "done", "cancel")
        )
        if (
            self.running
            and not goal_control
            and cmd
            not in (
                "/help",
                "/jobs",
                "/tokens",
                "/context",
                "/theme",
                "/usage",
                "/agents",
                "/teams",
                "/config",
            )
        ):
            raise ValueError("This command changes execution settings. Stop the turn first.")
        ctx = ShellContext(
            self.mgr,
            self.view.session_id,
            self.view.plan_mode,
            self.console,
            self.yes,
            self.pending_skills,
            self.prompt,
        )
        # Compatibility wizards suspend the PTK composer and release raw
        # mode explicitly. Their work runs in a worker, so job completion
        # and event delivery keep progressing on the shell loop.
        if cmd == "/jobs":
            from coderai.ui.shell.dispatch import cmd_jobs

            action = await asyncio.to_thread(cmd_jobs, ctx, args)
        elif cmd in ("/setup", "/login", "/logout", "/doctor", "/upgrade"):
            from coderai.ui.shell.dispatch import registry

            command = registry.find_command(cmd.lstrip("/"))
            if command is None or not callable(command.func):
                raise ValueError(f"Unavailable command: {cmd}")
            with self.prompt.session.app.input.cooked_mode():
                action = await asyncio.to_thread(command.func, ctx, args)
        else:
            action = await dispatch_slash_command(cmd, args, ctx)
        if ctx.session_id != self.view.session_id:
            self.switch(ctx.session_id)
        self.view.plan_mode = ctx.active_plan_mode
        self.prompt.update_plan_mode(self.view.plan_mode)
        if cmd == "/theme":
            self.preferences.theme = (
                "light" if args == "light" else "dark" if args == "dark" else self.preferences.theme
            )
            self.preferences.save(self.mgr.project_root)
        if action == SlashAction.TURN and ctx.turn_prompt:
            self.start(self.prepare(ctx.turn_prompt))
        elif action == SlashAction.EXIT:
            return False
    return True


async def execute_foreground_command(self: Any, raw: str) -> None:
    """Keep long slash operations under the same cancellation/queue owner."""
    from coderai.ui.shell.slash import parse_slash_command
    from coderai.ui.shell.dispatch import ShellContext, dispatch_slash_command

    if self.mgr.get_session(self.view.session_id) is None:
        self.view.session_id = await self.mgr.create_empty_session(plan_mode=self.view.plan_mode)
        self.presentation.switch(self.view.session_id)
        self.observe_session(self.view.session_id)
    ctx = ShellContext(
        self.mgr,
        self.view.session_id,
        self.view.plan_mode,
        self.console,
        self.yes,
        self.pending_skills,
        self.prompt,
    )
    cmd, args = parse_slash_command(raw)
    await dispatch_slash_command(cmd, args, ctx)


async def browse_activity(self: Any) -> None:
    sid = self.view.session_id
    store = getattr(self.mgr, "job_store", None)

    cached: list[BrowserRow] = []

    def snapshot():
        entry = self.mgr.get_session(sid) if sid else None
        status = self.view.phase.value if self.running or not entry else entry.status
        import time

        view = self.presentation
        elapsed = max(0, int((view.finished_at or time.monotonic()) - view.started_at))
        since_progress = max(0, int(time.monotonic() - view.last_progress))
        settings = self.mgr.get_resolved_settings()
        detail = f"Model: {self.mgr.get_active_model()}\nRole: {self.mgr.get_active_agent_role()}\nWorkspace: {self.mgr.project_root}\nState: {status}\nQueue: {len(self.view.queue)} ({'paused' if self.queue_paused else 'active'})\nElapsed: {elapsed}s\nLast progress: {since_progress}s ago\nActive tools: {', '.join(view.active_tools.values()) or 'none'}\nReasoning: {self.mgr.get_reasoning_effort()}\nPermissions: {json.dumps(settings.get('permissions', {}))}"
        result = [BrowserRow("foreground", f"Foreground: {status} | {elapsed}s", detail)]
        result.extend(
            BrowserRow(
                f"todo:{i}",
                f"Todo [{todo.get('status', 'pending')}]: {todo.get('content', todo.get('title', ''))}",
                json.dumps(todo, indent=2),
            )
            for i, todo in enumerate(view.todos)
            if isinstance(todo, dict)
        )
        if sid:
            from coderai.goals.core import get_goal_store

            result.extend(
                BrowserRow(
                    f"goal:{goal.id}",
                    f"Goal [{goal.status}] {goal.objective}",
                    f"Rounds: {goal.round}/{goal.max_rounds}\nMilestones: {goal.completed_milestones}/{len(goal.milestones)}\n{goal.notes}",
                )
                for goal in get_goal_store(self.mgr.project_root).list(sid)
            )
        if store and sid:
            result.extend(
                BrowserRow(job.id, f"{job.status} {job.label}", store.tail_output(job.id, sid))
                for job in store.snapshot(sid)
            )
        from coderai.subagents.core import get_agent_registry
        from coderai.ui.shell.agents_cmd import format_report

        registry = get_agent_registry()
        for handle in registry.list():
            root, seen = handle, set()
            while root.parent_agent_id and root.id not in seen:
                seen.add(root.id)
                parent = registry.get(root.parent_agent_id)
                if parent is None:
                    break
                root = parent
            if (
                sid
                and root.parent_session_id == sid
                and getattr(root.manager, "project_root", None) == self.mgr.project_root
            ):
                result.append(
                    BrowserRow(
                        f"agent:{handle.id}",
                        f"Agent {handle.id}: {handle.status}",
                        format_report(handle, handle.id),
                    )
                )
        return result

    async def poll():
        nonlocal cached
        while True:
            cached = await asyncio.to_thread(snapshot)
            self.invalidate()
            await asyncio.sleep(1)

    cached = await asyncio.to_thread(snapshot)
    updater = asyncio.create_task(poll(), name="shell-activity-snapshots")
    try:
        selected = await self.select("Live activity", lambda: cached)
    finally:
        updater.cancel()
        await asyncio.gather(updater, return_exceptions=True)
    if selected.value == "foreground" or str(selected.value).startswith(
        ("agent:", "todo:", "goal:")
    ):
        row = next((row for row in cached if row.id == selected.value), None)
        if row:
            await self.inspect(row.label, row.detail)
        return
    if (
        selected.cancelled
        or selected.value == "foreground"
        or str(selected.value).startswith("agent:")
    ):
        return
    if store is None or sid is None:
        return
    job = store.get(str(selected.value), sid)
    if not job:
        return
    action = await self.select(
        job.label,
        [BrowserRow("output", "Full output"), BrowserRow("stop", "Request cancellation")],
    )
    if action.value == "stop":
        confirm = await self.select(
            f"Stop {job.label}?",
            [BrowserRow("cancel", "Keep running"), BrowserRow("stop", "Request cancellation")],
        )
        if confirm.value == "stop":
            self.presentation.emit(await asyncio.to_thread(store.kill, job.id, sid))
    elif action.value == "output" and job.output_path:
        content = await asyncio.to_thread(
            Path(job.output_path).read_text, encoding="utf-8", errors="replace"
        )
        await self.inspect(job.label, content)


async def review_plan(self: Any) -> None:
    if self.running:
        raise ValueError("Wait for the plan turn to finish before applying it.")
    if not self.view.session_id:
        return
    from coderai.ui.shell.visualize._approval_panel import extract_plan_options

    entry = self.mgr.get_session(self.view.session_id)
    plan = self.mgr.read_plan(self.view.session_id) or entry.assistant_reply or ""
    options = extract_plan_options(plan)
    rows = [
        BrowserRow(f"option:{o['key']}", f"Approve option {o['key']}: {o['title']}", plan)
        for o in options
    ] or [BrowserRow("approve", "Approve plan and begin implementation", plan)]
    rows += [
        BrowserRow("revise", "Revise plan", plan),
        BrowserRow("reject", "Stay in plan mode", plan),
    ]
    choice = await self.select("Plan review", rows)
    if choice.cancelled or choice.value == "reject":
        return
    if choice.value == "revise":
        feedback = await self.ask("Revision notes: ")
        if feedback:
            self.start(self.prepare(feedback))
        return
    self.toggle_plan(False)
    self.prompt.update_plan_mode(False)
    self.start(
        self.prepare(
            "Implement this plan."
            if choice.value == "approve"
            else f"Implement option {str(choice.value).split(':')[1]} from this plan."
        )
    )
