"""NDJSON bridge between the Python agent and the Ink UI.

This module is intentionally decoupled from the Rich-based UI in
``coderAI/ui/*``. It subscribes to the same ``event_emitter`` signals and
serializes them as NDJSON lines to ``stdout``; the Ink UI parses those and
renders them with React/Ink components.

Commands from the UI arrive on ``stdin`` as NDJSON and are dispatched to the
agent via an internal async queue.

Wire format (see ``ui/PROTOCOL.md`` for the full spec):

    {"v": 1, "kind": "event", "event": "<name>", ...payload}
    {"v": 1, "kind": "cmd",   "cmd":   "<name>", "id": "...", ...payload}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import re
import sys
import time as _time
import uuid
from typing import Any, Awaitable, Callable, Dict, Optional

from ..agent_tracker import AgentInfo, AgentStatus, agent_tracker
from ..config import config_manager
from ..events import event_emitter
from ..system_prompt import _TOOL_SECTIONS

logger = logging.getLogger(__name__)

# Matches Rich-style markup tags, e.g. ``[bold cyan]``, ``[/bold cyan]``,
# ``[/]``. These are meant for the Rich one-shot CLI UI and must be stripped
# before crossing the NDJSON bridge to the Ink UI, which renders plain text.
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

    Kept server-side so every consumer (Ink UI, logs, CLI fallbacks) sees the
    same hint. The UI no longer tries to re-derive hints from message text.
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
            return "Rate limited — wait a few seconds and retry, or switch models with /model <name>."
        if "context" in lower and "length" in lower:
            return "Context window exceeded. Try /compact to summarize, or /clear to reset."
        if any(k in lower for k in ("quota", "insufficient", "billing")):
            return "Provider reports quota/billing exhausted. Top up credits or switch providers."
        if "timeout" in lower or "timed out" in lower:
            return "Request timed out. Try again; if persistent, check your network and /model."
        if any(k in lower for k in (
            "cannot connect", "connection refused", "econnrefused", "getaddrinfo",
        )):
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
    "File Operations": "fs",
    "Terminal": "shell",
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
    "run_command", "run_background", "write_file", "search_replace",
    "apply_diff", "git_commit", "git_checkout", "git_stash",
    "git_push", "git_reset", "git_rebase", "git_revert",
    "delete_file", "move_file", "kill_process",
}
_MEDIUM_RISK = {
    "delegate_task", "download_file", "mcp_call_tool",
    "git_merge", "git_cherry_pick", "copy_file", "http_request",
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
        "status": info.status.value if isinstance(info.status, AgentStatus)
                   else str(info.status),
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
    """Drives the NDJSON transport for the Ink UI.

    Usage:

        server = IPCServer(agent=agent)
        await server.run()   # returns when the UI sends {"cmd": "exit"}
                             # or stdin closes
    """

    def __init__(
        self,
        agent,
        *,
        stdin: Optional[asyncio.StreamReader] = None,
    ):
        self.agent = agent
        self._stdin_reader = stdin
        self._send_lock = asyncio.Lock()
        self._turn_lock = asyncio.Lock()
        self._exit = asyncio.Event()
        self._approval_waiters: Dict[str, asyncio.Future] = {}
        self._said_goodbye = False

        # Per-agent-id timestamp (ms) of the last forwarded ``agent_update``.
        # The tracker fires high-frequency token/cost ticks; we coalesce them
        # to ≤1Hz per agent so the UI panel doesn't thrash. Lifecycle
        # transitions bypass this throttle (see _on_agent_lifecycle).
        self._agent_update_last_ms: Dict[str, int] = {}

        # Verbosity filter — set by the UI via the ``set_verbosity`` command.
        # ``normal`` (default) emits the structured protocol but suppresses
        # the chattier ``success`` toasts. ``verbose`` re-enables them and
        # forwards ``agent_status`` narration. ``quiet`` additionally drops
        # transient ``info``/``warning`` toasts triggered by state changes
        # (long-form ``info`` payloads from ``/show`` are always passed
        # through — see ``_should_emit_event``).
        self._verbosity: str = "normal"

        # Track our own event_emitter subscriptions so we can detach them at
        # shutdown — the emitter is a module-level singleton, so a stale
        # IPCServer's listeners would otherwise keep firing after it exits.
        self._listener_refs: list[tuple[str, Callable[..., Any]]] = []

        # Bind event_emitter listeners once.
        self._wire_event_listeners()

    # -- outbound (stdout) ----------------------------------------------------

    def _write(self, payload: Dict[str, Any]) -> None:
        """Write one NDJSON line synchronously. Safe to call from any context."""
        try:
            sys.__stdout__.write(json.dumps(payload, default=str) + "\n")
            sys.__stdout__.flush()
        except (BrokenPipeError, ValueError):
            # UI closed the pipe; stop emitting.
            self._exit.set()

    def emit(self, event: str, **data: Any) -> None:
        """Emit one protocol event, honoring the current verbosity filter."""
        if not self._should_emit_event(event, data):
            return
        self._write({"v": 1, "kind": "event", "event": event, **data})

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

    def _emit_error(self, category: str, message: str, *,
                    hint: Optional[str] = None, details: Optional[str] = None) -> None:
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
            
        self.emit(
            "tool",
            id=tool_id,
            phase="awaiting_approval",
            payload=payload
        )
        timeout_s = int(getattr(self.agent.config, "approval_timeout_seconds", 300) or 0)
        try:
            if timeout_s > 0:
                return bool(await asyncio.wait_for(fut, timeout=timeout_s))
            return bool(await fut)
        except asyncio.TimeoutError:
            logger.warning(
                "Approval for %s timed out after %ss; denying.", tool_name, timeout_s
            )
            self.emit(
                "tool",
                id=tool_id,
                phase="cancelled",
                payload={"reason": "timeout", "timeoutSeconds": timeout_s}
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
        # "Calling tool…"). The Ink UI shows the same information via
        # tool_call / agent_update events, so we no longer forward it as
        # a persistent toast. Re-enabled only when verbose flag flips.
        _bind("agent_error", lambda message:
              self._emit_error("internal", _strip_rich_markup(message)))
        _bind("agent_paused", lambda message:
              self.emit("info", message=_strip_rich_markup(message)))
        _bind("agent_warning", lambda message:
              self.emit("warning", message=_strip_rich_markup(message)))
        _bind("file_diff", lambda path, diff:
              self.emit("file_diff", path=str(path), diff=str(diff)))
        _bind("agent_lifecycle", self._on_agent_lifecycle)
        _bind("agent_tracker_sync", self._on_agent_tracker_sync)

    def _unwire_event_listeners(self) -> None:
        for name, cb in self._listener_refs:
            event_emitter.off(name, cb)
        self._listener_refs.clear()

    def _on_tool_call(self, tool_name: str, arguments: Dict[str, Any], tool_id: str = None) -> None:
        # Use provided tool_id if available, otherwise generate one
        if not tool_id:
            tool_id = f"t_{uuid.uuid4().hex[:12]}"
        self._last_tool_id = tool_id  # fallback for callers not providing tool_id
        self.emit(
            "tool",
            id=tool_id,
            phase="running",
            payload={
                "name": tool_name,
                "category": _tool_category(tool_name, getattr(self.agent, "tools", None)),
                "args": _redact_args(arguments),
                "risk": _tool_risk(tool_name),
            }
        )

    def _on_tool_result(self, tool_name: str, result: Dict[str, Any], tool_id: str = None) -> None:
        if not tool_id:
            tool_id = getattr(self, "_last_tool_id", f"t_{uuid.uuid4().hex[:12]}")
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
            }
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



    # -- inbound (stdin) ------------------------------------------------------

    async def _read_commands(self) -> None:
        """Read NDJSON commands from stdin and dispatch them."""
        loop = asyncio.get_running_loop()

        # Bump the per-line limit from the 64 KB default so pasted prompts
        # or long tool_approval_resp frames don't trip LimitOverrunError.
        reader = asyncio.StreamReader(limit=4 * 1024 * 1024)
        protocol = asyncio.StreamReaderProtocol(reader)
        try:
            await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        except Exception as e:
            logger.error("Failed to hook stdin: %s", e)
            self._exit.set()
            return

        # Commands must dispatch concurrently: ``send_message`` holds the
        # coroutine open for the entire agentic turn (including awaiting
        # ``tool_approval_resp``), so if we awaited dispatch serially the
        # approval reply would never be read and the turn would deadlock.
        pending: set[asyncio.Task] = set()
        while not self._exit.is_set():
            try:
                line = await reader.readline()
            except asyncio.LimitOverrunError as e:
                # A frame exceeded the buffer. Drain it so we resync on the
                # next newline rather than wedging on the same byte.
                try:
                    await reader.readexactly(e.consumed)
                except Exception:
                    pass
                logger.warning("stdin: oversized NDJSON frame (%s bytes); dropped.", e.consumed)
                self.emit(
                    "error",
                    category="protocol",
                    message=f"Oversized NDJSON frame ({e.consumed} bytes) was dropped.",
                )
                continue
            except Exception as e:
                logger.error("stdin read failed: %s", e)
                break
            if not line:
                # EOF — the UI closed the pipe; shut down gracefully.
                self._exit.set()
                break
            decoded = line.decode("utf-8", errors="replace").strip()
            if not decoded:
                continue
            try:
                msg = json.loads(decoded)
            except json.JSONDecodeError as e:
                preview = decoded[:120] + ("…" if len(decoded) > 120 else "")
                logger.warning("stdin: malformed NDJSON frame (%s): %s", e, preview)
                self.emit(
                    "error",
                    category="protocol",
                    message=f"Malformed NDJSON frame ignored: {e.msg} at col {e.colno}",
                )
                continue
            if msg.get("kind") != "cmd":
                continue
            task = asyncio.create_task(self._dispatch(msg))
            pending.add(task)
            task.add_done_callback(pending.discard)

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

    async def run(self) -> None:
        """Run until the UI exits or stdin closes."""
        self.emit_hello()
        self.emit_ready()
        # Flush any agents already in the tracker (e.g. the idle root entry
        # seeded by entry.py) so the Agents panel is populated from boot
        # rather than only after the first turn registers a new agent.
        for info in agent_tracker.get_all():
            self.emit(
                "agent",
                phase="update",
                info=_agent_info_dict(info),
                parentId=info.parent_id,
            )
        reader_task = asyncio.create_task(self._read_commands())
        try:
            await self._exit.wait()
        finally:
            reader_task.cancel()
            try:
                await reader_task
            except (asyncio.CancelledError, Exception):
                pass
            if not self._said_goodbye:
                self.emit("goodbye")
                self._said_goodbye = True
            # Detach event_emitter listeners so a subsequent IPCServer
            # instance (e.g. in tests) doesn't receive duplicate events
            # from this one's lingering closures.
            self._unwire_event_listeners()


# --- Argument / result sanitization -----------------------------------------

_ARG_PREVIEW_LIMIT = 240
_RESULT_PREVIEW_LIMIT = 400


def _redact_args(args: Dict[str, Any]) -> Dict[str, Any]:
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


def _align_provider_usage_counters(
    provider: Any,
    *,
    prompt_tokens: int,
    completion_tokens: int,
) -> None:
    """Keep provider-local cumulative usage aligned with agent totals."""
    if provider is None:
        return
    if hasattr(provider, "total_input_tokens"):
        provider.total_input_tokens = max(0, int(prompt_tokens))
    if hasattr(provider, "total_output_tokens"):
        provider.total_output_tokens = max(0, int(completion_tokens))


# --- Command handlers -------------------------------------------------------

async def _cmd_send_message(server: IPCServer, msg: Dict[str, Any]) -> None:
    text = msg.get("text", "")
    stripped = str(text).strip()
    if stripped.startswith("/"):
        parts = stripped[1:].split(None, 1)
        cmd = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""
        allowlist = getattr(server.agent, "_tool_approval_allowlist", None)
        if allowlist is None:
            allowlist = set()
            server.agent._tool_approval_allowlist = allowlist
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
        server.emit("info", message=f"Cancelled {len(active)} active agent(s)")


async def _cmd_set_model(server: IPCServer, msg: Dict[str, Any]) -> None:
    model = msg.get("model", "")
    old_model = server.agent.model
    old_provider = server.agent.provider
    server.agent.model = model
    try:
        server.agent.provider = server.agent._create_provider()
    except Exception as e:
        server.agent.model = old_model
        server.agent.provider = old_provider
        server._emit_error("provider", f"Could not switch to {model}: {e}")
        return
    _align_provider_usage_counters(
        server.agent.provider,
        prompt_tokens=server.agent.total_prompt_tokens,
        completion_tokens=server.agent.total_completion_tokens,
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


async def _cmd_toggle_auto_approve(server: IPCServer, msg: Dict[str, Any]) -> None:
    server.agent.auto_approve = not server.agent.auto_approve
    server.agent._configure_delegate_tool_context()
    # Status bar's safe/YOLO pill is the indicator in normal mode; the
    # success toast surfaces only in verbose.
    server.emit("session_patch", autoApprove=bool(server.agent.auto_approve))
    server.emit(
        "success",
        message=(
            "Auto-approve enabled (YOLO)"
            if server.agent.auto_approve
            else "Auto-approve disabled"
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
    if waiter is not None and not waiter.done():
        waiter.set_result(approve)
    elif waiter is None:
        logger.warning(f"Late or invalid approval response for tool {tool_id}")
        server.emit("warning", message="Tool approval response was received too late.")


async def _cmd_clear_context(server: IPCServer, msg: Dict[str, Any]) -> None:
    server.agent.session = None
    server.agent.context_manager.clear()
    # create_session() calls _reset_session_accounting(), which zeros cost,
    # token counters, hook-approval cache, and the provider-side cumulative
    # usage so the new session is fully independent of the old one.
    server.agent.create_session()
    # The UI wipes its timeline on /clear and shows its own confirmation;
    # a server-side success toast adds an audit trail in verbose mode.
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
    cur_desc = (
        steps[current]["description"]
        if current < total
        else "All steps completed"
    )
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
    plan_path = Path(config.project_root).resolve() / ".coderAI" / "current_plan.json"
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


_COMMAND_HANDLERS: Dict[str, Callable[[IPCServer, Dict[str, Any]], Awaitable[None]]] = {
    "send_message": _cmd_send_message,
    "cancel": _cmd_cancel,
    "set_model": _cmd_set_model,
    "set_reasoning": _cmd_set_reasoning,
    "toggle_auto_approve": _cmd_toggle_auto_approve,
    "tool_approval_resp": _cmd_tool_approval_resp,
    "clear_context": _cmd_clear_context,
    "compact_context": _cmd_compact_context,
    "get_state": _cmd_get_state,
    "get_plan": _cmd_get_plan,
    "reference": _cmd_reference,
    "set_default_model": _cmd_set_default_model,
    "set_verbosity": _cmd_set_verbosity,
    "exit": _cmd_exit,
}
