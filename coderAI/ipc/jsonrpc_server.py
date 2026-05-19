"""Chat controller: agent events and command dispatch.

Drives the Textual UI in ``coderAI/tui/`` via an in-process callback
(``on_event``). The Textual app constructs an :class:`IPCServer`,
passes ``on_event`` to receive events on the UI thread, and pushes UI
intent back through :meth:`IPCServer.enqueue_command` /
:meth:`IPCServer.submit_command`.

Subscribes to ``event_emitter`` and dispatches UI commands to the agent.
See ``docs/CHAT_EVENTS.md`` for the event catalog.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import re
import time as _time
import uuid
from typing import Any, Awaitable, Callable, Dict, Optional

from ..agent_tracker import AgentInfo, AgentStatus, agent_tracker
from ..config import config_manager
from ..events import event_emitter
from ..project_layout import find_dot_coderai_subdir
from ..system_prompt import _TOOL_SECTIONS

logger = logging.getLogger(__name__)

# Strip Rich-style markup tags (e.g. ``[bold cyan]``, ``[/bold cyan]``,
# ``[/]``) from message payloads before emitting. Some legacy event sources
# inject Rich tags meant for the one-shot CLI; the Textual timeline renders
# these payloads as plain text, so the tags would otherwise leak through.
_RICH_TAG_RE = re.compile(r"\[/?[a-zA-Z][a-zA-Z0-9 _#\-/]*\]")


def _strip_rich_markup(text: Any) -> str:
    """Strip Rich markup tags from a string, returning plain text."""
    if text is None:
        return ""
    s = str(text)
    if "[" not in s:
        return s
    return _RICH_TAG_RE.sub("", s)


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


# --- Tool category inference ------------------------------------------------
#
# Primary source of truth is the ``category`` attribute on each ``Tool``
# subclass (see ``coderAI/tools/base.py``). The fallback map below covers
# MCP-proxy tools and anything that hasn't been tagged yet; tools looked
# up in the registry override the map.

_CATEGORY_MAP = {
    "File Operations": "filesystem",
    "Terminal": "terminal",
    "Git": "git",
    "Search & Analysis": "search",
    "Web": "web",
    "Memory (Persistent)": "memory",
    "Multi-Agent Delegation": "agent",
    "MCP (Model Context Protocol)": "mcp",
}

_TOOL_CATEGORY_FALLBACK = {}
for _section_name, _tool_names in _TOOL_SECTIONS:
    _cat = _CATEGORY_MAP.get(_section_name, "other")
    for _t in _tool_names:
        _TOOL_CATEGORY_FALLBACK[_t] = _cat

# Ensure MCP proxy tools are covered even if missing from main prompt list
_TOOL_CATEGORY_FALLBACK["mcp_connect"] = "mcp"
_TOOL_CATEGORY_FALLBACK["mcp_call_tool"] = "mcp"
_TOOL_CATEGORY_FALLBACK["mcp_list"] = "mcp"

_HIGH_RISK = {
    "run_command",
    "run_background",
    "write_file",
    "search_replace",
    "apply_diff",
    "git_commit",
    "git_checkout",
    "git_stash",
    "git_push",
    "git_reset",
    "git_rebase",
    "git_revert",
    "delete_file",
    "move_file",
    "kill_process",
}
_MEDIUM_RISK = {
    "delegate_task",
    "download_file",
    "mcp_call_tool",
    "git_merge",
    "git_cherry_pick",
    "copy_file",
    "http_request",
}


def _tool_category(name: str, registry: Optional[Any] = None) -> str:
    """Determine the UI category for ``name``.

    Prefers the ``category`` attribute on the tool instance (if the registry
    is supplied and the tool is registered). Falls back to the hardcoded
    map for tools that haven't been tagged or for MCP-proxy names.
    """
    if registry is not None:
        tool = registry.get(name)
        if tool is not None:
            cat = getattr(tool, "category", None)
            if cat and cat != "other":
                return cat
    return _TOOL_CATEGORY_FALLBACK.get(name, "other")


def _tool_risk(name: str) -> str:
    if name in _HIGH_RISK:
        return "high"
    if name in _MEDIUM_RISK:
        return "medium"
    return "low"


def _truncate_args(args: Dict[str, Any], limit: int, *, show_count: bool = False) -> Dict[str, Any]:
    """Shrink large string arg values so the UI stays snappy."""
    if not isinstance(args, dict):
        return {"value": str(args)[:limit]}
    out: Dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > limit:
            suffix = f"… ({len(v)} chars total)" if show_count else "…"
            out[k] = v[:limit] + suffix
        else:
            out[k] = v
    return out


def _preview_args_for_approval(arguments: Dict[str, Any]) -> Dict[str, Any]:
    return _truncate_args(arguments, 800, show_count=True)


# --- AgentInfo serialization -------------------------------------------------


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
    }


# --- The server -------------------------------------------------------------


class IPCServer:
    """In-process controller for the Textual chat UI.

    Usage:

        server = IPCServer(agent=agent, on_event=ui_callback)
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
        # IPCServer's listeners would otherwise keep firing after it exits.
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

        payload = {
            "name": tool_name,
            "args": _preview_args_for_approval(arguments),
            "risk": _tool_risk(tool_name),
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
        self.emit(
            "status",
            ctxUsed=used,
            ctxLimit=limit,
            costUsd=cost,
            budgetUsd=getattr(self.agent.config, "budget_limit", 0.0) or 0.0,
            promptTokens=getattr(self.agent, "total_prompt_tokens", 0),
            completionTokens=getattr(self.agent, "total_completion_tokens", 0),
            totalTokens=getattr(self.agent, "total_tokens", 0),
        )

    # -- event_emitter wiring -------------------------------------------------

    def _wire_event_listeners(self) -> None:
        em = event_emitter

        def _bind(name: str, cb: Callable[..., Any]) -> None:
            em.on(name, cb)
            self._listener_refs.append((name, cb))

        _bind("tool_call", self._on_tool_call)
        _bind("tool_result", self._on_tool_result)
        _bind("tool_error", self._emit_tool_error)
        # `agent_status` is high-frequency narration ("Reading file…",
        # "Calling tool…"). The Textual UI shows the same information via
        # tool_call / agent_update events, so we no longer forward it as
        # a persistent toast. Re-enabled only when verbose flag flips.
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


# --- Argument / result sanitization -----------------------------------------

_ARG_PREVIEW_LIMIT = 240
_RESULT_PREVIEW_LIMIT = 400


def _arg_preview(args: Dict[str, Any]) -> Dict[str, Any]:
    """Truncate string values to keep tool-call args panel snappy."""
    return _truncate_args(args, _ARG_PREVIEW_LIMIT)


def _result_preview(result: Dict[str, Any]) -> str:
    """Pick the most useful one-line preview we can out of a tool result."""
    if not isinstance(result, dict):
        return str(result)[:_RESULT_PREVIEW_LIMIT]

    # Common fields, in priority order.
    for key in ("summary", "preview", "message", "output", "content", "path"):
        val = result.get(key)
        if val:
            s = str(val).splitlines()[0] if isinstance(val, str) else str(val)
            if len(s) > _RESULT_PREVIEW_LIMIT:
                s = s[:_RESULT_PREVIEW_LIMIT] + "…"
            return s

    # Fallback: count-like summaries for search/fs tools.
    for key in ("count", "matches", "file_count"):
        if key in result:
            return f"{result[key]} {key.replace('_', ' ')}"

    # Last resort: short stringification.
    s = str({k: v for k, v in result.items() if k not in ("success", "error")})
    return s[:_RESULT_PREVIEW_LIMIT]


# --- Persona / skills slash helpers ----------------------------------------


def _handle_persona_slash(server: "IPCServer", arg: str) -> None:
    """Inline ``/persona [name|default|list]`` handler.

    - ``/persona``           — list available personas (also when arg=="list")
    - ``/persona default``   — clear the active persona (also: ``none``, ``off``)
    - ``/persona <name>``    — switch to the named persona (filename stem)
    """
    from ..agents import get_available_personas, resolve_persona_name

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


def _handle_skills_slash(server: "IPCServer") -> None:
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


async def _cmd_send_message(server: IPCServer, msg: Dict[str, Any]) -> None:
    text = msg.get("text", "")
    stripped = str(text).strip()
    if stripped.startswith("/"):
        parts = stripped[1:].split(None, 1)
        cmd = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""
        allowlist = getattr(server.agent, "_tool_approval_allowlist", set())
        if cmd == "allow-tool":
            if not arg:
                server.emit("warning", message="Usage: /allow-tool <tool-name>")
                server.emit_ready()
                return
            allowlist.add(arg)
            server.emit("info", message=f"Tool approval memory enabled for {arg}.")
            server.emit_ready()
            return
        if cmd == "disallow-tool":
            if not arg:
                server.emit("warning", message="Usage: /disallow-tool <tool-name>")
                server.emit_ready()
                return
            allowlist.discard(arg)
            server.emit("info", message=f"Tool approval memory removed for {arg}.")
            server.emit_ready()
            return
        if cmd == "allowed-tools":
            names = ", ".join(sorted(allowlist)) if allowlist else "(none)"
            server.emit("info", message=f"Always-allowed tools for this session: {names}")
            server.emit_ready()
            return
        if cmd == "persona":
            _handle_persona_slash(server, arg)
            server.emit_ready()
            return
        if cmd == "skills":
            _handle_skills_slash(server)
            server.emit_ready()
            return
    async with server._turn_lock:
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


async def _cmd_cancel(server: IPCServer, msg: Dict[str, Any]) -> None:
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


async def _cmd_set_model(server: IPCServer, msg: Dict[str, Any]) -> None:
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


async def _cmd_set_persona(server: IPCServer, msg: Dict[str, Any]) -> None:
    """Switch the active persona programmatically (used by future UI picker).

    Payload: ``{"persona": "<name>"}``; empty/omitted/``"default"`` clears it.
    """
    raw = msg.get("persona") or (msg.get("payload") or {}).get("persona") or ""
    _handle_persona_slash(server, str(raw).strip())
    server.emit_ready()


async def _cmd_toggle_auto_approve(server: IPCServer, msg: Dict[str, Any]) -> None:
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


async def _cmd_set_reasoning(server: IPCServer, msg: Dict[str, Any]) -> None:
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


async def _cmd_tool_approval_resp(server: IPCServer, msg: Dict[str, Any]) -> None:
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


async def _cmd_clear_context(server: IPCServer, msg: Dict[str, Any]) -> None:
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


async def _cmd_compact_context(server: IPCServer, msg: Dict[str, Any]) -> None:
    try:
        await server.agent.compact_context()
    except Exception as e:
        server._emit_error("internal", f"Compaction failed: {e}")
    else:
        server.emit("success", message="Context compacted")
    server.emit_status()


async def _cmd_get_state(server: IPCServer, msg: Dict[str, Any]) -> None:
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


async def _cmd_get_plan(server: IPCServer, msg: Dict[str, Any]) -> None:
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
    server.emit("info", message=_format_plan_message(plan))


async def _cmd_list_personas(server: IPCServer, _msg: Dict[str, Any]) -> None:
    from ..agents import get_available_personas

    project_root = getattr(server.agent.config, "project_root", ".")
    available = get_available_personas(project_root)
    current = server.agent.persona.name if server.agent.persona else None
    server.emit("available_personas", current=current, personas=sorted(available))


async def _cmd_list_skills(server: IPCServer, _msg: Dict[str, Any]) -> None:
    from ..tools.skills import get_available_skills

    project_root = getattr(server.agent.config, "project_root", ".")
    skills = get_available_skills(project_root)
    server.emit("available_skills", skills=skills)


async def _cmd_search_codebase(server: IPCServer, msg: Dict[str, Any]) -> None:
    query = msg.get("query", "")
    if not query:
        return
    try:
        from ..cli.search import semantic_search

        results = semantic_search(
            query, project_root=getattr(server.agent.config, "project_root", "."), n_results=10
        )
        if not results:
            server.emit("info", message=f"No semantic search results found for '{query}'.")
            return
        out = [f"Semantic search results for '{query}':\n"]
        for r in results:
            preview = r.content.strip().split(chr(10))[0][:80]
            out.append(f"• {r.filepath} (score: {r.score:.2f})\n  {preview}...")
        server.emit("info", message="\n".join(out))
    except Exception as e:
        server.emit("warning", message=f"Codebase search failed: {e}")


async def _cmd_list_models(server: IPCServer, _msg: Dict[str, Any]) -> None:
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


async def _cmd_reference(server: IPCServer, msg: Dict[str, Any]) -> None:
    """Emit long-form help text (models, cost, system status, config, info, tasks)."""
    from .chat_reference import build_tasks_text, resolve_reference_text

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


async def _cmd_set_default_model(server: IPCServer, msg: Dict[str, Any]) -> None:
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


async def _cmd_set_verbosity(server: IPCServer, msg: Dict[str, Any]) -> None:
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


async def _cmd_exit(server: IPCServer, msg: Dict[str, Any]) -> None:
    server.emit("goodbye", reason="user")
    server._said_goodbye = True
    server._exit.set()


async def _cmd_cancel_agent(server: IPCServer, msg: Dict[str, Any]) -> None:
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


_COMMAND_HANDLERS: Dict[str, Callable[[IPCServer, Dict[str, Any]], Awaitable[None]]] = {
    "send_message": _cmd_send_message,
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
