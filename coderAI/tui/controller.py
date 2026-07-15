"""In-process chat controller (Textual UI bridge).

Drives the Textual UI in ``coderAI/tui/`` via an in-process callback
(``on_event``). The Textual app constructs an :class:`UIBridge`,
passes ``on_event`` to receive events on the UI thread, and pushes UI
intent back through :meth:`UIBridge.enqueue_command` /
:meth:`UIBridge.submit_command`.

Subscribes to ``event_emitter`` and dispatches UI commands to the agent.
See ``docs/CHAT_EVENTS.md`` for the event catalog.

The command handlers live in ``coderAI/tui/commands.py`` and the payload
serializers in ``coderAI/tui/serializers.py``; both are
re-exported here because tests and callers historically import them from
this module — and because tests patch module attributes such as
``coderAI.tui.controller.agent_tracker`` that the handlers resolve
through this namespace at call time.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import time as _time
import uuid
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from coderAI.core.agent_tracker import AgentInfo, agent_tracker
from coderAI.system.events import event_emitter

if TYPE_CHECKING:
    from coderAI.core.agent import Agent

from coderAI.tui.commands import (  # noqa: F401
    _cmd_allow_tool,
    _cmd_cancel,
    _cmd_cancel_agent,
    _cmd_clear_context,
    _cmd_compact_context,
    _cmd_disallow_tool,
    _cmd_exit,
    _cmd_get_state,
    _cmd_get_tasks,
    _cmd_init_project,
    _cmd_list_allowed_tools,
    _cmd_list_models,
    _cmd_list_personas,
    _cmd_list_skills,
    _cmd_manage_context,
    _cmd_reference,
    _cmd_rewind,
    _cmd_search_codebase,
    _cmd_send_message,
    _cmd_set_default_model,
    _cmd_set_model,
    _cmd_set_persona,
    _cmd_set_reasoning,
    _cmd_set_verbosity,
    _cmd_set_auto_approve,
    _cmd_toggle_auto_approve,
    _cmd_tool_approval_resp,
    _COMMAND_HANDLERS,
    _handle_persona_slash,
)
from coderAI.tui.serializers import (  # noqa: F401
    _agent_info_dict,
    _compute_agent_depth,
    _infer_error_hint,
    _load_tasks_from_disk,
    _serialize_tasks_for_ui,
    _task_ui_item,
)
from coderAI.tui.tool_metadata import (
    arg_preview,
    parse_skill_steps,
    preview_args_for_approval,
    result_preview,
    tool_category,
    tool_risk,
    tool_risk_factors,
)
from coderAI.tui.rendering import strip_rich_markup

logger = logging.getLogger(__name__)

# --- The server -------------------------------------------------------------


class UIBridge:
    """In-process controller for the Textual chat UI.

    Usage:

        server = UIBridge(agent=agent, on_event=ui_callback)
        await server.start()   # returns when ``request_shutdown`` is called
    """

    def __init__(
        self,
        agent: "Agent",
        *,
        on_event: Callable[[str, Dict[str, Any]], None],
    ) -> None:
        self.agent = agent
        self._on_event = on_event
        self._turn_lock = asyncio.Lock()
        self._exit = asyncio.Event()
        self._approval_waiters: Dict[str, asyncio.Future] = {}
        self._pending_tasks: set[asyncio.Task] = set()
        self._said_goodbye = False
        self._session_start_ts: float = 0.0
        self._iteration: int = 0
        # Captured at start() so command-enqueue calls from other threads
        # (the Textual UI runs on a different thread than the agent loop)
        # can schedule work on the agent's running loop.
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Per-agent-id timestamp (ms) of the last forwarded ``agent_update``.
        # The tracker fires high-frequency token/cost ticks; we coalesce them
        # to ≤1Hz per agent so the UI panel doesn't thrash. Lifecycle
        # transitions bypass this throttle (see _on_agent_lifecycle).
        self._agent_update_last_ms: Dict[str, int] = {}
        self._agent_update_prune_ct: int = 0

        # Verbosity filter — set by the UI via the ``set_verbosity`` command.
        # ``normal`` (default) emits the structured protocol but suppresses
        # the chattier ``success`` toasts. ``verbose`` re-enables them.
        # ``quiet`` additionally drops transient ``info``/``warning`` toasts
        # triggered by state changes (long-form ``info`` payloads from ``/show``
        # are always passed through — see ``_should_emit_event``).
        self._verbosity: str = "normal"

        # Track our own event_emitter subscriptions so we can detach them at
        # shutdown — the emitter is a module-level singleton, so a stale
        # UIBridge's listeners would otherwise keep firing after it exits.
        self._listener_refs: list[tuple[str, Callable[..., Any]]] = []

        # Bind event_emitter listeners once.
        self._wire_event_listeners()

    # -- outbound -------------------------------------------------------------

    def emit(self, event: str, **data: Any) -> None:
        """Emit one event to the UI, honoring the current verbosity filter."""
        if not self._should_emit_event(event, data):
            return
        try:
            self._on_event(event, dict(data))
        except Exception:
            logger.exception("on_event callback failed for %s", event)

    def _should_emit_event(self, event: str, data: Dict[str, Any]) -> bool:
        """Decide whether ``event`` survives the current verbosity filter.

        Structural protocol events (hello/ready/goodbye/tool_*/file_diff/
        status/agent_*/error/*_changed/etc.) are always passed through —
        only the chatty ``info``/``warning``/``success`` toasts get
        filtered, and even then only when they look like single-line
        state announcements.
        """
        v = self._verbosity
        if v == "verbose":
            return True
        if event == "success":
            # `success` events are always state-change toasts — never
            # carry user-requested reference output.
            return False
        if event in ("info", "warning"):
            msg = str(data.get("message", ""))
            # Long-form payloads (multi-line) are reference output from
            # `/show` and `/tasks` — keep them at every level.
            if "\n" in msg:
                return True
            return v != "quiet"
        return True

    def _emit_error(
        self,
        category: str,
        message: str,
        *,
        hint: Optional[str] = None,
        details: Optional[str] = None,
    ) -> None:
        """Emit an ``error`` event with a canonical hint if one isn't supplied."""
        payload: Dict[str, Any] = {"category": category, "message": message}
        resolved_hint = hint if hint is not None else _infer_error_hint(category, message)
        if resolved_hint:
            payload["hint"] = resolved_hint
        if details:
            payload["details"] = details
        self.emit("error", **payload)

    def _emit_tool_error(self, tool_name: str, error: Any) -> None:
        self._emit_error("tool", strip_rich_markup(f"{tool_name}: {error}"))

    async def request_tool_approval(
        self,
        tool_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        diff: Optional[str] = None,
    ) -> bool:
        """Block until the UI sends ``tool_approval_resp`` for this tool call.

        Timed out on ``config.approval_timeout_seconds`` (default 300s, set to
        0 to wait forever). On timeout the tool is denied and the UI is told
        so it can clear the pending prompt.
        """
        if not tool_id:
            tool_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._approval_waiters[tool_id] = fut

        # Build requester info for the enhanced approval modal
        tracker_info = getattr(self.agent, "tracker_info", None)
        requested_by = tracker_info.name if tracker_info else "main"
        parent_id = tracker_info.parent_id if tracker_info else None
        iteration = self._iteration
        prior_approved = self._count_prior_approved_this_turn()

        timeout_s = int(getattr(self.agent.config, "approval_timeout_seconds", 300) or 0)
        remember = self._approval_memory_option(tool_name, arguments)
        payload = {
            "name": tool_name,
            "args": preview_args_for_approval(arguments),
            "risk": tool_risk(tool_name, getattr(self.agent, "tools", None)),
            "riskFactors": tool_risk_factors(tool_name, getattr(self.agent, "tools", None)),
            "requestedBy": requested_by,
            "parentId": parent_id,
            "iteration": iteration,
            "maxIterations": getattr(self.agent.config, "max_iterations", 50),
            "priorApproved": prior_approved,
            "timeoutSeconds": timeout_s,
            "expiresAt": (_time.time() + timeout_s) if timeout_s > 0 else None,
            **remember,
        }
        if diff is not None:
            payload["diff"] = diff

        self.emit("tool", id=tool_id, phase="awaiting_approval", payload=payload)
        try:
            if timeout_s > 0:
                return bool(await asyncio.wait_for(fut, timeout=timeout_s))
            return bool(await fut)
        except asyncio.TimeoutError:
            logger.warning("Approval for %s timed out after %ss; denying.", tool_name, timeout_s)
            self.emit(
                "tool",
                id=tool_id,
                phase="cancelled",
                payload={"reason": "timeout", "timeoutSeconds": timeout_s},
            )
            return False
        except asyncio.CancelledError:
            if not fut.done():
                fut.cancel()
            raise
        finally:
            # Drop any leftover waiter (handler normally pops on approve/deny).
            self._approval_waiters.pop(tool_id, None)

    def _approval_memory_option(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Describe the narrowest safe session approval the modal may remember.

        The enforcement source remains :class:`ApprovalRules`; this method only
        exposes a candidate scope already visible in the approval request. Unknown
        and non-tool prompts deliberately receive no remember action.
        """
        registry = getattr(self.agent, "tools", None)
        tool = registry.get(tool_name) if registry is not None else None
        if tool is None:
            return {}

        scope_kind = getattr(tool, "approval_scope", None)
        scope = ""
        if scope_kind == "command":
            scope = str(arguments.get("command") or "").strip()
        elif scope_kind == "path":
            scope = str(arguments.get("path") or arguments.get("file_path") or "").strip()

        if scope:
            noun = "command prefix" if scope_kind == "command" else "path"
            return {
                "rememberMode": "scope",
                "rememberScope": scope,
                "rememberLabel": f"Allow this {noun}",
            }

        if not bool(getattr(tool, "high_risk_no_blanket", False)):
            return {
                "rememberMode": "tool",
                "rememberScope": "",
                "rememberLabel": f"Allow {tool_name} this session",
            }
        return {}

    def _count_prior_approved_this_turn(self) -> int:
        """Count how many tools were already approved in the current turn."""
        count = 0
        track = getattr(self.agent, "tracker_info", None)
        if track:
            for info in agent_tracker.get_all():
                if info.parent_id == track.agent_id and info.status not in (
                    "done",
                    "error",
                    "cancelled",
                    "idle",
                ):
                    count += 1
        return count

    # -- lifecycle helpers ----------------------------------------------------

    def emit_hello(self) -> None:
        config = self.agent.config

        self.emit(
            "hello",
            model=self.agent.model,
            provider=self.agent.provider.__class__.__name__,
            cwd=os.getcwd(),
            version=getattr(self.agent, "version", "0.1.0"),
            contextLimit=getattr(config, "context_window", 200000),
            budgetLimit=getattr(config, "budget_limit", 0.0) or 0.0,
            autoApprove=bool(getattr(self.agent, "auto_approve", False)),
            reasoning=str(getattr(config, "reasoning_effort", "none") or "none"),
        )

    def emit_ready(self) -> None:
        self.emit("ready")

    def emit_session_replay(self) -> None:
        """Replay persisted transcript messages into the TUI timeline."""
        session = getattr(self.agent, "session", None)
        if session is None:
            return
        messages = []
        for message in session.messages:
            if message.role == "system":
                continue
            messages.append(
                {
                    "role": message.role,
                    "content": message.content,
                    "timestamp": message.timestamp,
                    "tool_calls": message.tool_calls,
                    "tool_call_id": message.tool_call_id,
                    "name": message.name,
                    "reasoning_content": message.reasoning_content,
                }
            )
        if messages:
            self.emit("session_replay", messages=messages)

    def emit_status(self) -> None:
        try:
            used, limit = self.agent.get_context_usage()
        except Exception:
            # Status-bar decoration only; a broken gauge must not break the UI loop.
            logger.debug("get_context_usage failed in emit_status", exc_info=True)
            used, limit = 0, 0
        cost = 0.0
        try:
            cost = self.agent.cost_tracker.get_total_cost()
        except Exception:
            # Same: show $0.00 rather than failing the status event.
            logger.debug("cost_tracker failed in emit_status", exc_info=True)
        elapsed = _time.time() - self._session_start_ts if self._session_start_ts else 0.0
        workspace_trusted = True
        try:
            from coderAI.system.trust import workspace_trust

            root = getattr(self.agent.config, "project_root", ".") or "."
            # Only surface an "untrusted" pill when there is a surface to trust;
            # a plain repo with no .coderAI automation should read as neutral.
            if workspace_trust.has_execution_surface(root):
                workspace_trusted = workspace_trust.is_trusted(root)
        except Exception:
            logger.debug("workspace trust lookup failed in emit_status", exc_info=True)
        self.emit(
            "status",
            ctxUsed=used,
            ctxLimit=limit,
            workspaceTrusted=workspace_trusted,
            costUsd=cost,
            budgetUsd=getattr(self.agent.config, "budget_limit", 0.0) or 0.0,
            promptTokens=getattr(self.agent, "total_prompt_tokens", 0),
            completionTokens=getattr(self.agent, "total_completion_tokens", 0),
            totalTokens=getattr(self.agent, "total_tokens", 0),
            iteration=self._iteration,
            maxIterations=getattr(self.agent.config, "max_iterations", 50),
            elapsedSeconds=elapsed,
        )

    def tick_iteration(self) -> None:
        self._iteration += 1

    # -- event_emitter wiring -------------------------------------------------

    def _wire_event_listeners(self) -> None:
        em = event_emitter

        def _bind(name: str, cb: Callable[..., Any]) -> None:
            em.on(name, cb)
            self._listener_refs.append((name, cb))

        _bind("tool_call", self._on_tool_call)
        _bind("tool_result", self._on_tool_result)
        _bind("tool_error", self._emit_tool_error)
        _bind("agent_status", self._on_agent_status)
        _bind(
            "agent_error", lambda message: self._emit_error("internal", strip_rich_markup(message))
        )
        _bind("agent_paused", lambda message: self.emit("info", message=strip_rich_markup(message)))
        _bind(
            "agent_warning",
            lambda message: self.emit("warning", message=strip_rich_markup(message)),
        )
        _bind(
            "file_diff", lambda path, diff: self.emit("file_diff", path=str(path), diff=str(diff))
        )
        _bind("agent_lifecycle", self._on_agent_lifecycle)
        _bind("agent_tracker_sync", self._on_agent_tracker_sync)
        _bind("tool_progress", self._on_tool_progress)
        _bind("tasks_update", self._on_tasks_update)

    def _unwire_event_listeners(self) -> None:
        for name, cb in self._listener_refs:
            event_emitter.off(name, cb)
        self._listener_refs.clear()

    def _on_agent_status(self, message: str) -> None:
        if self._verbosity == "verbose":
            self.emit("info", message=strip_rich_markup(message))

    def _on_tool_call(
        self, tool_name: str, arguments: Dict[str, Any], tool_id: Optional[str] = None
    ) -> None:
        # Use provided tool_id if available, otherwise generate one
        if not tool_id:
            tool_id = f"t_{uuid.uuid4().hex[:12]}"
        self.emit(
            "tool",
            id=tool_id,
            phase="running",
            payload={
                "name": tool_name,
                "category": tool_category(tool_name, getattr(self.agent, "tools", None)),
                "args": arg_preview(arguments),
                "risk": tool_risk(tool_name, getattr(self.agent, "tools", None)),
            },
        )

    def _on_tool_result(
        self, tool_name: str, result: Dict[str, Any], tool_id: Optional[str] = None
    ) -> None:
        if not tool_id:
            tool_id = f"t_{uuid.uuid4().hex[:12]}"
        ok = bool(result.get("success", True))
        error = result.get("error") if not ok else None
        preview = result_preview(result)
        self.emit(
            "tool",
            id=tool_id,
            phase="ok" if ok else "err",
            payload={
                "preview": preview,
                "fullAvailable": len(str(result)) > len(preview),
                "error": error,
            },
        )
        if tool_name == "use_skill" and ok:
            skill_name = str(result.get("skill_name") or "")
            skill_desc = str(result.get("description") or "")
            instructions = str(result.get("instructions") or "")
            steps = parse_skill_steps(instructions)
            if skill_name or steps:
                self.emit(
                    "skill_card",
                    id=tool_id,
                    name=skill_name,
                    description=skill_desc,
                    steps=steps,
                )

    def _on_agent_lifecycle(self, action: str, info: AgentInfo) -> None:
        # Lifecycle transitions are rare and load-bearing — never throttled.
        # Reset the per-agent throttle clock so the next tick goes through.
        self._agent_update_last_ms.pop(info.agent_id, None)
        self.emit(
            "agent",
            phase=action,
            info=_agent_info_dict(info),
            parentId=info.parent_id,
        )

    def _on_agent_tracker_sync(self, info: AgentInfo) -> None:
        """Push live token/cost/task updates for main + sub-agents to the UI.

        Coalesced per-agent to ≤1 emission/second — the tracker fires on
        every tool call and stream tick, which is ~10× more than the UI
        needs. Terminal status transitions still flow through
        ``agent_lifecycle``.
        """
        # Always pass through terminal states so the panel sees the final
        # token/cost numbers without waiting out the throttle window.
        terminal = info.status in ("done", "error", "cancelled")
        if not terminal:
            now_ms = int(_time.time() * 1000)
            last_ms = self._agent_update_last_ms.get(info.agent_id, 0)
            if now_ms - last_ms < 1000:
                return
            self._agent_update_last_ms[info.agent_id] = now_ms
        else:
            self._agent_update_last_ms.pop(info.agent_id, None)
        self._agent_update_prune_ct += 1
        if self._agent_update_prune_ct % 30 == 0:
            self._prune_stale_agent_entries()
        # Also prune whenever we add a new entry and the dict has grown large
        elif len(self._agent_update_last_ms) > 64:
            self._prune_stale_agent_entries()
        self.emit("agent", phase="update", info=_agent_info_dict(info), parentId=info.parent_id)

    def _prune_stale_agent_entries(self) -> None:
        now_ms = int(_time.time() * 1000)
        stale = [aid for aid, last in self._agent_update_last_ms.items() if now_ms - last > 60000]
        for aid in stale:
            self._agent_update_last_ms.pop(aid, None)

    def _on_tool_progress(
        self,
        step: int,
        total: int,
        tool_name: str,
        elapsed: Optional[float] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "label": tool_name,
            "current": step,
            "total": total,
            "progressKind": "steps",
        }
        if elapsed is not None:
            payload["elapsed"] = elapsed
        self.emit("progress", **payload)

    def _on_tasks_update(self, tasks: Optional[List[Dict[str, Any]]] = None) -> None:
        self.emit("tasks_card", tasks=_serialize_tasks_for_ui(tasks or []))

    def _emit_tasks_from_disk(self) -> None:
        pr = getattr(self.agent.config, "project_root", None) or "."
        tasks = _load_tasks_from_disk(str(pr))
        self.emit("tasks_card", tasks=_serialize_tasks_for_ui(tasks))

    # -- inbound --------------------------------------------------------------

    async def _dispatch(self, msg: Dict[str, Any]) -> None:
        cmd = msg.get("cmd")
        if not isinstance(cmd, str):
            self.emit("warning", message=f"Invalid command format: {cmd}")
            return
        handler = _COMMAND_HANDLERS.get(cmd)
        if handler is None:
            self.emit("warning", message=f"Unknown command: {cmd}")
            return
        try:
            await handler(self, msg)
        except Exception as e:
            logger.exception("Command %s failed", cmd)
            self._emit_error("internal", f"{cmd} failed: {e}")

    # -- main loop ------------------------------------------------------------

    def enqueue_command(self, cmd: str, *, cmd_id: Optional[str] = None, **fields: Any) -> None:
        """Schedule a UI command for async dispatch.

        Safe to call from any thread. The agent runs in a worker thread with
        its own event loop; callers from the Textual UI thread reach the
        loop via :py:meth:`asyncio.AbstractEventLoop.call_soon_threadsafe`.
        """
        msg: Dict[str, Any] = {
            "kind": "cmd",
            "cmd": cmd,
            "id": cmd_id or str(uuid.uuid4()),
        }
        msg.update(fields)

        def _schedule() -> None:
            task = asyncio.create_task(self._dispatch(msg))
            self._pending_tasks.add(task)
            task.add_done_callback(self._pending_tasks.discard)

        loop = self._loop
        if loop is None or not loop.is_running():
            # No loop yet (very early bootstrap) — try the current thread.
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                logger.warning("Dropping command %s: no event loop available", cmd)
                return
            _schedule()
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            _schedule()
        else:
            loop.call_soon_threadsafe(_schedule)

    async def submit_command(
        self, cmd: str, *, cmd_id: Optional[str] = None, **fields: Any
    ) -> None:
        """Dispatch one command and await completion.

        Safe to call from the Textual UI thread. Approval replies must run on
        the agent loop that owns the waiter Future; dispatching on the UI loop
        leaves the turn asleep until a later ``enqueue_command`` wakes it.
        """
        msg: Dict[str, Any] = {
            "kind": "cmd",
            "cmd": cmd,
            "id": cmd_id or str(uuid.uuid4()),
        }
        msg.update(fields)

        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if loop is None or not loop.is_running() or running is loop:
            await self._dispatch(msg)
            return

        done: concurrent.futures.Future[None] = concurrent.futures.Future()

        def _schedule() -> None:
            async def _run() -> None:
                try:
                    await self._dispatch(msg)
                except Exception as exc:
                    if not done.done():
                        done.set_exception(exc)
                else:
                    if not done.done():
                        done.set_result(None)

            task = asyncio.create_task(_run())
            self._pending_tasks.add(task)

            def _cleanup(t: asyncio.Task) -> None:
                self._pending_tasks.discard(t)
                if t.cancelled() and not done.done():
                    done.cancel()

            task.add_done_callback(_cleanup)

        loop.call_soon_threadsafe(_schedule)
        await asyncio.wrap_future(done)

    def request_shutdown(self, *, reason: str = "user") -> None:
        """Signal the UI to exit."""
        if not self._said_goodbye:
            self.emit("goodbye", reason=reason)
            self._said_goodbye = True
        self._exit.set()

    async def _bootstrap_session(self) -> None:
        """Emit hello/ready and seed the agent tree."""
        self._session_start_ts = _time.time()
        self.emit_hello()
        self.emit_session_replay()
        self.emit_status()
        self.emit_ready()
        for info in agent_tracker.get_all():
            self.emit(
                "agent",
                phase="update",
                info=_agent_info_dict(info),
                parentId=info.parent_id,
            )
        self._emit_tasks_from_disk()

    async def _shutdown(self) -> None:
        for task in list(self._pending_tasks):
            task.cancel()
        if self._pending_tasks:
            await asyncio.gather(*self._pending_tasks, return_exceptions=True)
        self._resolve_approval_waiters_on_shutdown()
        if not self._said_goodbye:
            self.emit("goodbye")
            self._said_goodbye = True
        self._unwire_event_listeners()

    async def start(self) -> None:
        """Bootstrap the session and wait until ``request_shutdown`` is called."""
        self._loop = asyncio.get_running_loop()
        await self._bootstrap_session()
        try:
            await self._exit.wait()
        finally:
            await self._shutdown()

    def _resolve_approval_waiters_on_shutdown(self) -> None:
        """Resolve any pending tool-approval futures when the server shuts down."""
        for tool_id, fut in list(self._approval_waiters.items()):
            if not fut.done():
                fut.set_result(False)
        self._approval_waiters.clear()

    def _cancel_pending_approvals(self, reason: str) -> int:
        """Deny and wake all pending approval prompts."""
        cancelled = 0
        for tool_id, fut in list(self._approval_waiters.items()):
            if fut.done():
                continue
            fut.set_result(False)
            cancelled += 1
            self.emit(
                "tool",
                id=tool_id,
                phase="cancelled",
                payload={"reason": reason},
            )
        return cancelled
