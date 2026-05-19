"""In-process chat controller (Textual UI bridge).

Drives the Textual UI in ``coderAI/tui/`` via an in-process callback
(``on_event``). The Textual app constructs an :class:`UIBridge`,
passes ``on_event`` to receive events on the UI thread, and pushes UI
intent back through :meth:`UIBridge.enqueue_command` /
:meth:`UIBridge.submit_command`.

Subscribes to ``event_emitter`` and dispatches UI commands to the agent.
See ``docs/CHAT_EVENTS.md`` for the event catalog.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import time as _time
import uuid
from typing import Any, Awaitable, Callable, Dict, Optional

from coderAI.core.agent_tracker import AgentInfo, AgentStatus, agent_tracker
from coderAI.system.config import config_manager
from coderAI.system.events import event_emitter
from coderAI.system.project_layout import find_dot_coderai_subdir

from coderAI.bridge.tool_metadata import (
    arg_preview,
    parse_skill_steps,
    preview_args_for_approval,
    result_preview,
    strip_rich_markup,
    tool_category,
    tool_risk,
)

logger = logging.getLogger(__name__)

_strip_rich_markup = strip_rich_markup
_tool_category = tool_category
_tool_risk = tool_risk
_parse_skill_steps = parse_skill_steps
_preview_args_for_approval = preview_args_for_approval
_arg_preview = arg_preview
_result_preview = result_preview


def _infer_error_hint(category: str, message: str) -> Optional[str]:
    """Best-effort canonical hint for an error message.

    Kept server-side so every consumer (Textual UI, logs, CLI fallbacks)
    sees the same hint. The UI no longer tries to re-derive hints from
    message text.
    """
    lower = (message or "").lower()
    if category == "provider":
        if "localhost:1234" in lower or "lmstudio" in lower:
            return "Start LM Studio: open the app → Developer → Start Server."
        if "localhost:11434" in lower or "ollama" in lower:
            return "Start Ollama: run `ollama serve` in another terminal."
        if "anthropic" in lower and "key" in lower:
            return "Set ANTHROPIC_API_KEY, or run `coderAI config set anthropic_api_key <KEY>`."
        if "openai" in lower and "key" in lower:
            return "Set OPENAI_API_KEY, or run `coderAI config set openai_api_key <KEY>`."
        if "groq" in lower and "key" in lower:
            return "Set GROQ_API_KEY, or run `coderAI config set groq_api_key <KEY>`."
        if "deepseek" in lower and "key" in lower:
            return "Set DEEPSEEK_API_KEY, or run `coderAI config set deepseek_api_key <KEY>`."
        if any(k in lower for k in ("api key", "401", "unauthorized", "authentication")):
            return "Missing/invalid API key — run `coderAI setup` or `coderAI doctor`."
        if any(k in lower for k in ("rate limit", "429", "too many requests")):
            return (
                "Rate limited — wait a few seconds and retry, or switch models with /model <name>."
            )
        if "context" in lower and "length" in lower:
            return "Context window exceeded. Try /compact to summarize, or /clear to reset."
        if any(k in lower for k in ("quota", "insufficient", "billing")):
            return "Provider reports quota/billing exhausted. Top up credits or switch providers."
        if "timeout" in lower or "timed out" in lower:
            return "Request timed out. Try again; if persistent, check your network and /model."
        if any(
            k in lower
            for k in (
                "cannot connect",
                "connection refused",
                "econnrefused",
                "getaddrinfo",
            )
        ):
            return "Network/service unreachable. Check the endpoint URL, DNS, and firewall."
        if "ssl" in lower or "certificate" in lower:
            return "TLS handshake failed. Check your system clock and corporate proxy/CA certs."
    if category == "tool":
        if any(k in lower for k in ("permission denied", "eacces", "eperm")):
            return "The tool lacks filesystem permissions. Check file ownership/mode."
        if "not found" in lower or "enoent" in lower:
            return "Target path or command wasn't found. Double-check the argument."
        if "timeout" in lower or "timed out" in lower:
            return "Tool timed out. For long shells try run_background, or raise timeout in args."
        if "cancelled" in lower or "cancel" in lower:
            return "Cancelled."
    return None


def _agent_info_dict(info: AgentInfo) -> Dict[str, Any]:
    return {
        "id": info.agent_id,
        "name": info.name,
        "role": info.role,
        "parentId": info.parent_id,
        "status": info.status.value if isinstance(info.status, AgentStatus) else str(info.status),
        "task": info.current_task,
        "tool": info.current_tool,
        "model": info.model,
        "tokens": info.total_tokens,
        "costUsd": info.cost_usd,
        "ctxUsed": info.context_used_tokens,
        "ctxLimit": info.context_limit_tokens,
        "elapsedMs": int(info.elapsed_seconds * 1000),
        "depth": _compute_agent_depth(info),
    }


def _compute_agent_depth(info: AgentInfo) -> int:
    depth = 0
    pid = info.parent_id
    while pid:
        parent = agent_tracker.get(pid)
        if parent is None:
            break
        depth += 1
        pid = parent.parent_id
    return depth


# --- The server -------------------------------------------------------------


class UIBridge:
    """In-process controller for the Textual chat UI.

    Usage:

        server = UIBridge(agent=agent, on_event=ui_callback)
        await server.start()   # returns when ``request_shutdown`` is called
    """

    def __init__(
        self,
        agent,
        *,
        on_event: Callable[[str, Dict[str, Any]], None],
    ):
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
            # `/show`, `/tasks`, `/plan` — keep them at every level.
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
        self._emit_error("tool", _strip_rich_markup(f"{tool_name}: {error}"))

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

        payload = {
            "name": tool_name,
            "args": _preview_args_for_approval(arguments),
            "risk": _tool_risk(tool_name),
            "requestedBy": requested_by,
            "parentId": parent_id,
            "iteration": iteration,
            "maxIterations": getattr(self.agent.config, "max_iterations", 50),
            "priorApproved": prior_approved,
        }
        if diff is not None:
            payload["diff"] = diff

        self.emit("tool", id=tool_id, phase="awaiting_approval", payload=payload)
        timeout_s = int(getattr(self.agent.config, "approval_timeout_seconds", 300) or 0)
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

    def emit_status(self) -> None:
        try:
            used, limit = self.agent.get_context_usage()
        except Exception:
            used, limit = 0, 0
        cost = 0.0
        try:
            cost = self.agent.cost_tracker.get_total_cost()
        except Exception:
            pass
        elapsed = _time.time() - self._session_start_ts if self._session_start_ts else 0.0
        self.emit(
            "status",
            ctxUsed=used,
            ctxLimit=limit,
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
            "agent_error", lambda message: self._emit_error("internal", _strip_rich_markup(message))
        )
        _bind(
            "agent_paused", lambda message: self.emit("info", message=_strip_rich_markup(message))
        )
        _bind(
            "agent_warning",
            lambda message: self.emit("warning", message=_strip_rich_markup(message)),
        )
        _bind(
            "file_diff", lambda path, diff: self.emit("file_diff", path=str(path), diff=str(diff))
        )
        _bind("agent_lifecycle", self._on_agent_lifecycle)
        _bind("agent_tracker_sync", self._on_agent_tracker_sync)
        _bind("tool_progress", self._on_tool_progress)

    def _unwire_event_listeners(self) -> None:
        for name, cb in self._listener_refs:
            event_emitter.off(name, cb)
        self._listener_refs.clear()

    def _on_agent_status(self, message: str) -> None:
        if self._verbosity == "verbose":
            self.emit("info", message=_strip_rich_markup(message))

    def _on_tool_call(self, tool_name: str, arguments: Dict[str, Any], tool_id: str = None) -> None:
        # Use provided tool_id if available, otherwise generate one
        if not tool_id:
            tool_id = f"t_{uuid.uuid4().hex[:12]}"
        self.emit(
            "tool",
            id=tool_id,
            phase="running",
            payload={
                "name": tool_name,
                "category": _tool_category(tool_name, getattr(self.agent, "tools", None)),
                "args": _arg_preview(arguments),
                "risk": _tool_risk(tool_name),
            },
        )

    def _on_tool_result(self, tool_name: str, result: Dict[str, Any], tool_id: str = None) -> None:
        if not tool_id:
            tool_id = f"t_{uuid.uuid4().hex[:12]}"
        ok = bool(result.get("success", True))
        error = result.get("error") if not ok else None
        preview = _result_preview(result)
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
            steps = _parse_skill_steps(instructions)
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
        self.emit("agent", phase="update", info=_agent_info_dict(info), parentId=info.parent_id)

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

    # -- inbound --------------------------------------------------------------

    async def _dispatch(self, msg: Dict[str, Any]) -> None:
        cmd = msg.get("cmd")
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
        """Dispatch one command and await completion."""
        msg: Dict[str, Any] = {
            "kind": "cmd",
            "cmd": cmd,
            "id": cmd_id or str(uuid.uuid4()),
        }
        msg.update(fields)
        await self._dispatch(msg)

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
        self.emit_ready()
        for info in agent_tracker.get_all():
            self.emit(
                "agent",
                phase="update",
                info=_agent_info_dict(info),
                parentId=info.parent_id,
            )

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


# --- Persona / skills slash helpers ----------------------------------------


def _handle_persona_slash(server: "UIBridge", arg: str) -> None:
    """Inline ``/persona [name|default|list]`` handler.

    - ``/persona``           — list available personas (also when arg=="list")
    - ``/persona default``   — clear the active persona (also: ``none``, ``off``)
    - ``/persona <name>``    — switch to the named persona (filename stem)
    """
    from coderAI.core.agents import get_available_personas, resolve_persona_name

    project_root = getattr(server.agent.config, "project_root", ".")
    available = get_available_personas(project_root)

    name = (arg or "").strip().lower()
    if not name or name == "list":
        if not available:
            server.emit(
                "info",
                message=(
                    "No personas found in .coderAI/agents/. "
                    "Create <stem>.md files with YAML frontmatter to define one."
                ),
            )
            return
        current = server.agent.persona.name if server.agent.persona else "(default)"
        listing = "\n".join(f"  • {n}" for n in sorted(available))
        server.emit(
            "info",
            message=f"Available personas (current: {current}):\n{listing}\n\nUse /persona <name> to switch · /persona default to clear.",
        )
        return

    if name in ("default", "none", "off", "clear"):
        server.agent.set_persona(None)
        server.emit("session_patch", model=server.agent.model)
        server.emit("info", message="Persona cleared — back to the default agent.")
        return

    resolved = resolve_persona_name(arg, project_root)
    if not resolved:
        hint = f"Persona '{arg}' not found. Available: {', '.join(sorted(available)) or '(none)'}"
        server.emit("warning", message=hint)
        return

    applied = server.agent.set_persona(resolved)
    if applied is None:
        server.emit("warning", message=f"Failed to apply persona '{resolved}'.")
        return
    # Persona may carry a model override that ``set_persona`` activated; surface
    # the patch so the UI status bar refreshes.
    server.emit(
        "session_patch",
        model=server.agent.model,
        provider=server.agent.provider.__class__.__name__,
    )
    server.emit("info", message=f"Persona switched → {applied.name}")


def _handle_skills_slash(server: "UIBridge") -> None:
    """Inline ``/skills`` — list workflows found under ``.coderAI/skills/``."""
    from ..tools.skills import get_available_skills

    project_root = getattr(server.agent.config, "project_root", ".")
    skills = get_available_skills(project_root)
    if not skills:
        server.emit(
            "info",
            message=(
                "No skills found in .coderAI/skills/. "
                "Create <name>.md files with YAML frontmatter to define skill workflows."
            ),
        )
        return
    listing = "\n".join(f"  • {s['name']} — {s['description']}" for s in skills)
    server.emit(
        "info",
        message=(
            f"Available skills ({len(skills)}):\n{listing}\n\n"
            "Ask the agent to run one, e.g. 'use the security-audit skill'."
        ),
    )


# --- Command handlers -------------------------------------------------------


async def _cmd_send_message(server: UIBridge, msg: Dict[str, Any]) -> None:
    text = msg.get("text", "")
    async with server._turn_lock:
        server.tick_iteration()
        try:
            await server.agent.process_message(text)
        except Exception as e:
            server._emit_error(
                "internal",
                str(e),
                hint="See logs on stderr for the full traceback.",
            )
        finally:
            server.emit_status()
            server.emit_ready()


async def _cmd_cancel(server: UIBridge, msg: Dict[str, Any]) -> None:
    approvals_cancelled = server._cancel_pending_approvals("cancelled_by_user")
    agent_id = msg.get("agentId")
    if agent_id:
        ok = agent_tracker.cancel(agent_id)
        if ok:
            server.emit("info", message=f"Cancelled agent {agent_id[-8:]}")
        else:
            server.emit("warning", message=f"No active agent {agent_id}")
    else:
        active = agent_tracker.get_active()
        agent_tracker.cancel_all()
        suffix = f" and {approvals_cancelled} pending approval(s)" if approvals_cancelled else ""
        server.emit("info", message=f"Cancelled {len(active)} active agent(s){suffix}")


async def _cmd_set_model(server: UIBridge, msg: Dict[str, Any]) -> None:
    model = msg.get("model", "")
    old_model = server.agent.model
    old_provider = server.agent.provider
    server.agent.model = model
    try:
        server.agent.provider = server.agent._create_provider()
        context_controller = getattr(server.agent, "context_controller", None)
        if context_controller is not None:
            context_controller.provider = server.agent.provider
    except Exception as e:
        server.agent.model = old_model
        server.agent.provider = old_provider
        context_controller = getattr(server.agent, "context_controller", None)
        if context_controller is not None:
            context_controller.provider = old_provider
        server._emit_error("provider", f"Could not switch to {model}: {e}")
        return
    server.agent.provider.set_cumulative_usage(
        input_tokens=server.agent.total_prompt_tokens,
        output_tokens=server.agent.total_completion_tokens,
    )
    server.agent._configure_delegate_tool_context()
    # Persist the hot-switch on the active session so replays from
    # ``~/.coderAI/history/`` report the model that was actually used for
    # each turn from this point forward.
    if server.agent.session is not None:
        server.agent.session.model = model
    server.emit("session_patch", model=model, provider=server.agent.provider.__class__.__name__)
    # Verbose-only confirmation; the status bar carries the change in normal mode.
    server.emit("success", message=f"Switched model → {model}")


async def _cmd_allow_tool(server: UIBridge, msg: Dict[str, Any]) -> None:
    tool = str(msg.get("tool", "")).strip()
    if not tool:
        server.emit("warning", message="Usage: /allow-tool <tool-name>")
        return
    allowlist = getattr(server.agent, "_tool_approval_allowlist", set())
    allowlist.add(tool)
    server.emit("info", message=f"Tool approval memory enabled for {tool}.")


async def _cmd_disallow_tool(server: UIBridge, msg: Dict[str, Any]) -> None:
    tool = str(msg.get("tool", "")).strip()
    if not tool:
        server.emit("warning", message="Usage: /disallow-tool <tool-name>")
        return
    allowlist = getattr(server.agent, "_tool_approval_allowlist", set())
    allowlist.discard(tool)
    server.emit("info", message=f"Tool approval memory removed for {tool}.")


async def _cmd_list_allowed_tools(server: UIBridge, _msg: Dict[str, Any]) -> None:
    allowlist = getattr(server.agent, "_tool_approval_allowlist", set())
    names = ", ".join(sorted(allowlist)) if allowlist else "(none)"
    server.emit("info", message=f"Always-allowed tools for this session: {names}")


async def _cmd_set_persona(server: UIBridge, msg: Dict[str, Any]) -> None:
    """Switch the active persona programmatically (used by future UI picker).

    Payload: ``{"persona": "<name>"}``; empty/omitted/``"default"`` clears it.
    """
    raw = msg.get("persona") or (msg.get("payload") or {}).get("persona") or ""
    _handle_persona_slash(server, str(raw).strip())
    server.emit_ready()


async def _cmd_toggle_auto_approve(server: UIBridge, msg: Dict[str, Any]) -> None:
    server.agent.auto_approve = not server.agent.auto_approve
    server.agent._configure_delegate_tool_context()
    # Status bar's safe/YOLO pill is the indicator in normal mode; the
    # success toast surfaces only in verbose.
    server.emit("session_patch", autoApprove=bool(server.agent.auto_approve))
    server.emit(
        "success",
        message=(
            "Auto-approve enabled (YOLO)" if server.agent.auto_approve else "Auto-approve disabled"
        ),
    )


async def _cmd_set_reasoning(server: UIBridge, msg: Dict[str, Any]) -> None:
    effort = str(msg.get("effort", "none")).lower()
    if effort not in ("high", "medium", "low", "none"):
        server.emit(
            "warning",
            message=f"Invalid reasoning effort: {effort!r}. Use high|medium|low|none.",
        )
        return
    # Persist in config so the next provider creation inherits the value.
    server.agent.config.reasoning_effort = effort
    # Also patch the live provider directly so the change takes effect on the
    # very next LLM call without requiring a model switch or restart.
    provider = getattr(server.agent, "provider", None)
    if provider is not None:
        try:
            setattr(provider, "reasoning_effort", effort)
        except Exception:
            pass
    server.emit("session_patch", reasoning=effort)
    # Status bar shows current reasoning level; no toast.


async def _cmd_tool_approval_resp(server: UIBridge, msg: Dict[str, Any]) -> None:
    tool_id = str(msg.get("toolId") or msg.get("id") or "")
    approve = bool(msg.get("approve", False))
    waiter = server._approval_waiters.pop(tool_id, None)
    if waiter is None:
        logger.warning("Late or invalid approval response for tool %s", tool_id)
        server.emit("warning", message="Tool approval response was received too late.")
        return
    # Calling ``set_result`` on an already-resolved or cancelled future would
    # raise ``InvalidStateError``. The waiter can complete out from under us
    # via timeout (``asyncio.wait_for``) or cancellation (``/clear``, ``/exit``)
    # before the UI's response arrives, so check first.
    if waiter.done():
        logger.warning(
            "Approval response for tool %s arrived after waiter resolved (state=%s); ignoring.",
            tool_id,
            "cancelled" if waiter.cancelled() else "done",
        )
        server.emit("warning", message="Tool approval response was received too late.")
        return
    waiter.set_result(approve)


async def _cmd_clear_context(server: UIBridge, msg: Dict[str, Any]) -> None:
    async with server._turn_lock:
        server.agent.session = None
        server.agent.context_manager.clear()
        server.agent.create_session()
    main_info = getattr(server.agent, "tracker_info", None)
    if main_info is not None:
        agent_tracker.clear_except({main_info.agent_id})
        main_info.status = AgentStatus.IDLE
        main_info.current_task = ""
        main_info.current_tool = None
        main_info.finished_at = None
        server.emit(
            "agent",
            phase="update",
            info=_agent_info_dict(main_info),
            parentId=main_info.parent_id,
        )
    else:
        agent_tracker.clear_except()
    server.emit("success", message="Session cleared")
    server.emit_status()


async def _cmd_compact_context(server: UIBridge, msg: Dict[str, Any]) -> None:
    try:
        await server.agent.compact_context()
    except Exception as e:
        server._emit_error("internal", f"Compaction failed: {e}")
    else:
        server.emit("success", message="Context compacted")
    server.emit_status()


async def _cmd_get_state(server: UIBridge, msg: Dict[str, Any]) -> None:
    server.emit_status()
    for info in agent_tracker.get_all():
        server.emit("agent", phase="update", info=_agent_info_dict(info), parentId=info.parent_id)

    context_files = []
    pinned = server.agent.context_manager.pinned_files
    for path_str, content in pinned.items():
        context_files.append({"path": path_str, "size": len(content)})
    server.emit("context_state", files=context_files)


def _format_plan_message(plan: Dict[str, Any]) -> str:
    """Plain-text summary for UI toast (matches `plan` tool show semantics)."""
    title = plan.get("title") or "Untitled"
    steps = plan.get("steps") or []
    total = len(steps)
    try:
        current = int(plan.get("current_step", 0))
    except (TypeError, ValueError):
        current = 0
    completed = sum(1 for s in steps if s.get("status") == "done")
    cur_desc = steps[current]["description"] if current < total else "All steps completed"
    lines = [
        f"Plan: {title}",
        f"Progress: {completed}/{total} steps · current: {cur_desc}",
        "",
    ]
    for i, s in enumerate(steps):
        mark = "✓" if s.get("status") == "done" else "○"
        prefix = "→" if i == current and i < total else " "
        desc = s.get("description", "")
        lines.append(f"  {prefix}{mark} {i + 1}. {desc}")
    return "\n".join(lines)


def _serialize_plan_for_ui(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize plan data into a structured payload for the plan_card UI event."""
    steps = plan.get("steps") or []
    total = len(steps)
    try:
        current = int(plan.get("current_step", 0))
    except (TypeError, ValueError):
        current = 0
    completed = sum(1 for s in steps if s.get("status") == "done")
    return {
        "title": plan.get("title", "Untitled"),
        "completed": completed,
        "total": total,
        "currentIdx": current,
        "steps": [
            {
                "index": i + 1,
                "description": s.get("description", ""),
                "status": s.get("status", "pending"),
            }
            for i, s in enumerate(steps)
        ],
    }


async def _cmd_get_plan(server: UIBridge, msg: Dict[str, Any]) -> None:
    pr = getattr(server.agent.config, "project_root", None) or "."
    config = config_manager.load_project_config(pr)
    dot_coderai_dir = find_dot_coderai_subdir("", str(config.project_root))
    if dot_coderai_dir is None:
        dot_coderai_dir = Path(config.project_root).resolve() / ".coderAI"
    plan_path = dot_coderai_dir / "current_plan.json"
    if not plan_path.exists():
        server.emit(
            "info",
            message="No active execution plan. The agent can create one with the plan tool.",
        )
        return
    try:
        with open(plan_path, "r", encoding="utf-8") as f:
            plan = json.load(f)
    except Exception as e:
        server.emit("warning", message=f"Could not read plan: {e}")
        return
    if not isinstance(plan, dict):
        server.emit("warning", message="Invalid plan file.")
        return
    server.emit("plan_card", plan=_serialize_plan_for_ui(plan))
    server.emit("info", message=_format_plan_message(plan))


async def _cmd_list_personas(server: UIBridge, _msg: Dict[str, Any]) -> None:
    from coderAI.core.agents import get_available_personas

    project_root = getattr(server.agent.config, "project_root", ".")
    available = get_available_personas(project_root)
    current = server.agent.persona.name if server.agent.persona else None
    server.emit("available_personas", current=current, personas=sorted(available))


async def _cmd_list_skills(server: UIBridge, _msg: Dict[str, Any]) -> None:
    from ..tools.skills import get_available_skills

    project_root = getattr(server.agent.config, "project_root", ".")
    skills = get_available_skills(project_root)
    server.emit("available_skills", skills=skills)


async def _cmd_search_codebase(server: UIBridge, msg: Dict[str, Any]) -> None:
    query = msg.get("query", "")
    if not query:
        return
    try:
        from ..embeddings.factory import create_embedding_provider
        from coderAI.context.code_indexer import CodeIndexer

        project_root = getattr(server.agent.config, "project_root", ".")
        config = config_manager.load()
        provider = create_embedding_provider(config)
        if provider is None:
            server.emit(
                "warning",
                message="No embedding provider available for code search. Set openai_api_key.",
            )
            return
        indexer = CodeIndexer(str(Path(project_root).resolve()), provider)
        results = await indexer.search(query=query, top_k=10)
        if not results:
            server.emit("info", message=f"No semantic search results found for '{query}'.")
            return
        out = [f"Semantic search results for '{query}':\n"]
        for r in results:
            snippet = r["text"].strip().split("\n")[0][:80]
            out.append(
                f"• {r['file_path']} L{r['start_line']}-{r['end_line']} (score: {r['score']:.2f})\n  {snippet}..."
            )
        server.emit("info", message="\n".join(out))
    except Exception as e:
        server.emit("warning", message=f"Codebase search failed: {e}")


async def _cmd_list_models(server: UIBridge, _msg: Dict[str, Any]) -> None:
    """Return all available models grouped by provider for the model-picker UI."""
    from ..llm.anthropic import MODEL_ALIASES
    from ..llm.deepseek import DeepSeekProvider
    from ..llm.groq import GroqProvider
    from ..llm.openai import OpenAIProvider

    server.emit(
        "available_models",
        current=server.agent.model,
        models={
            "Anthropic": sorted(MODEL_ALIASES.keys()),
            "OpenAI": sorted(OpenAIProvider.SUPPORTED_MODELS.keys()),
            "DeepSeek": sorted(DeepSeekProvider.SUPPORTED_MODELS.keys()),
            "Groq": sorted(GroqProvider.SUPPORTED_MODELS.keys()),
            "Local": ["lmstudio", "ollama"],
        },
    )


async def _cmd_reference(server: UIBridge, msg: Dict[str, Any]) -> None:
    """Emit long-form help text (models, cost, system status, config, info, tasks)."""
    from coderAI.bridge.chat_reference import build_tasks_text, resolve_reference_text

    topic = str(msg.get("topic", "")).strip()
    if not topic:
        server.emit(
            "warning",
            message="Missing topic. Try /version, /models, /cost, /system, /config, /info, /tasks.",
        )
        return
    t = topic.lower()
    if t in ("tasks", "todos", "task"):
        pr = getattr(server.agent.config, "project_root", None) or "."
        try:
            text = await build_tasks_text(pr)
        except Exception as e:
            server.emit("warning", message=f"Tasks: {e}")
            return
        server.emit("info", message=text)
        return
    try:
        text = resolve_reference_text(t, server.agent)
    except ValueError as e:
        server.emit("warning", message=str(e))
        return
    except Exception as e:
        server.emit("warning", message=f"Reference failed: {e}")
        return
    server.emit("info", message=text)


async def _cmd_set_default_model(server: UIBridge, msg: Dict[str, Any]) -> None:
    """Persist default_model in global config (like ``coderAI set-model``)."""
    from ..llm.anthropic import MODEL_ALIASES
    from ..llm.deepseek import DeepSeekProvider
    from ..llm.groq import GroqProvider
    from ..llm.openai import OpenAIProvider

    model_name = str(msg.get("model") or "").strip()
    if not model_name:
        server.emit("warning", message="Usage: /default <model>")
        return

    valid_models = (
        list(OpenAIProvider.SUPPORTED_MODELS.keys())
        + list(MODEL_ALIASES.keys())
        + list(GroqProvider.SUPPORTED_MODELS.keys())
        + list(DeepSeekProvider.SUPPORTED_MODELS.keys())
        + ["lmstudio", "ollama"]
    )
    if model_name not in valid_models:
        server.emit(
            "warning",
            message=(
                f"Invalid model name: {model_name}. "
                "Use /models for groups; names must match provider IDs exactly."
            ),
        )
        return
    config_manager.set("default_model", model_name)
    current = server.agent.model
    if current != model_name:
        server.emit(
            "info",
            message=(
                f"Saved default model → {model_name}. "
                f"Current session is still using {current}; "
                f"use /model {model_name} to switch now."
            ),
        )
    else:
        server.emit(
            "info",
            message=f"Saved default model → {model_name} (already active).",
        )


async def _cmd_set_verbosity(server: UIBridge, msg: Dict[str, Any]) -> None:
    """Adjust the IPC server's event filter.

    Levels (least → most chatty):
      - quiet:   drop info/warning/success state toasts entirely.
      - normal:  drop success toasts only (default).
      - verbose: pass through everything including agent_status narration.
    """
    level = str(msg.get("level", "normal")).lower()
    if level not in ("quiet", "normal", "verbose"):
        server.emit(
            "warning",
            message=f"Invalid verbosity: {level!r}. Use quiet|normal|verbose.",
        )
        return
    server._verbosity = level


async def _cmd_exit(server: UIBridge, msg: Dict[str, Any]) -> None:
    server.emit("goodbye", reason="user")
    server._said_goodbye = True
    server._exit.set()


async def _cmd_cancel_agent(server: UIBridge, msg: Dict[str, Any]) -> None:
    """Cancel a specific sub-agent by ID."""
    agent_id = (msg.get("payload") or {}).get("agentId")
    if not agent_id:
        server.emit("error", category="protocol", message="cancel_agent requires agentId")
        return
    cancelled = agent_tracker.cancel(agent_id)
    server.emit(
        "success",
        message=f"Sub-agent {agent_id} cancellation {'requested' if cancelled else 'failed (not found)'}",
    )


_COMMAND_HANDLERS: Dict[str, Callable[[UIBridge, Dict[str, Any]], Awaitable[None]]] = {
    "send_message": _cmd_send_message,
    "allow_tool": _cmd_allow_tool,
    "disallow_tool": _cmd_disallow_tool,
    "list_allowed_tools": _cmd_list_allowed_tools,
    "cancel": _cmd_cancel,
    "cancel_agent": _cmd_cancel_agent,
    "set_model": _cmd_set_model,
    "set_reasoning": _cmd_set_reasoning,
    "set_persona": _cmd_set_persona,
    "toggle_auto_approve": _cmd_toggle_auto_approve,
    "tool_approval_resp": _cmd_tool_approval_resp,
    "clear_context": _cmd_clear_context,
    "compact_context": _cmd_compact_context,
    "get_state": _cmd_get_state,
    "get_plan": _cmd_get_plan,
    "list_models": _cmd_list_models,
    "list_personas": _cmd_list_personas,
    "list_skills": _cmd_list_skills,
    "search_codebase": _cmd_search_codebase,
    "reference": _cmd_reference,
    "set_default_model": _cmd_set_default_model,
    "set_verbosity": _cmd_set_verbosity,
    "exit": _cmd_exit,
}
