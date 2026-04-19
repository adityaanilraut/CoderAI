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
import re
import sys
import uuid
from typing import Any, Awaitable, Callable, Dict, Optional

from ..agent_tracker import AgentInfo, AgentStatus, agent_tracker
from ..events import event_emitter

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


# --- Tool category inference ------------------------------------------------

_TOOL_CATEGORIES = {
    # filesystem
    "read_file": "fs", "write_file": "fs", "search_replace": "fs",
    "apply_diff": "fs", "list_directory": "fs", "glob_search": "fs",
    # search
    "text_search": "search", "grep": "search",
    # git
    "git_add": "git", "git_status": "git", "git_diff": "git",
    "git_commit": "git", "git_log": "git", "git_branch": "git",
    "git_checkout": "git", "git_stash": "git",
    # terminal
    "run_command": "shell", "run_background": "shell",
    # web
    "web_search": "web", "read_url": "web", "download_file": "web",
    # subagent
    "delegate_task": "agent",
    # mcp
    "mcp_connect": "mcp", "mcp_call": "mcp", "mcp_list": "mcp",
}

_HIGH_RISK = {"run_command", "run_background", "write_file", "search_replace",
              "apply_diff", "git_commit", "git_checkout", "git_stash"}
_MEDIUM_RISK = {"delegate_task", "download_file", "mcp_call"}


def _tool_category(name: str) -> str:
    return _TOOL_CATEGORIES.get(name, "other")


def _tool_risk(name: str) -> str:
    if name in _HIGH_RISK:
        return "high"
    if name in _MEDIUM_RISK:
        return "medium"
    return "low"


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
        self._exit = asyncio.Event()
        self._approval_waiters: Dict[str, asyncio.Future] = {}

        # Bind event_emitter listeners once.
        self._wire_event_listeners()

    # -- outbound (stdout) ----------------------------------------------------

    def _write(self, payload: Dict[str, Any]) -> None:
        """Write one NDJSON line synchronously. Safe to call from any context."""
        try:
            sys.stdout.write(json.dumps(payload, default=str) + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, ValueError):
            # UI closed the pipe; stop emitting.
            self._exit.set()

    def emit(self, event: str, **data: Any) -> None:
        """Emit one protocol event."""
        self._write({"v": 1, "kind": "event", "event": event, **data})

    # -- lifecycle helpers ----------------------------------------------------

    def emit_hello(self) -> None:
        config = self.agent.config
        project_summary = None

        session_id = None
        message_count = 0
        try:
            if self.agent.session is not None:
                session_id = getattr(self.agent.session, "id", None)
                message_count = len(getattr(self.agent.session, "messages", []) or [])
        except Exception:
            pass

        self.emit(
            "hello",
            model=self.agent.model,
            provider=self.agent.provider.__class__.__name__,
            cwd=os.getcwd(),
            version=getattr(self.agent, "version", "0.1.0"),
            projectSummary=project_summary,
            contextLimit=getattr(config, "context_window", 200000),
            budgetLimit=getattr(config, "budget_limit", 0.0) or 0.0,
            autoApprove=bool(getattr(self.agent, "auto_approve", False)),
            sessionId=session_id,
            resumedMessageCount=message_count,
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

        em.on("tool_call", self._on_tool_call)
        em.on("tool_result", self._on_tool_result)
        em.on("tool_error", lambda tool_name, error:
              self.emit("error", category="tool",
                        message=_strip_rich_markup(f"{tool_name}: {error}")))
        em.on("agent_status", lambda message:
              self.emit("info", message=_strip_rich_markup(message)))
        em.on("agent_error", lambda message:
              self.emit("error", category="internal",
                        message=_strip_rich_markup(message)))
        em.on("agent_warning", lambda message:
              self.emit("warning", message=_strip_rich_markup(message)))
        em.on("file_diff", lambda path, diff:
              self.emit("file_diff", path=str(path), diff=str(diff)))
        em.on("status_start", lambda message=None:
              self.emit("thinking_start"))
        em.on("status_stop", lambda *a, **kw:
              self.emit("thinking_end", elapsedMs=0))
        em.on("agent_lifecycle", self._on_agent_lifecycle)

    def _on_tool_call(self, tool_name: str, arguments: Dict[str, Any]) -> None:
        # Generate a stable-ish id so we can match the subsequent tool_result.
        tool_id = f"t_{uuid.uuid4().hex[:12]}"
        self._last_tool_id = tool_id  # naive: assumes serial tool calls
        self.emit(
            "tool_call",
            id=tool_id,
            name=tool_name,
            category=_tool_category(tool_name),
            args=_redact_args(arguments),
            risk=_tool_risk(tool_name),
        )

    def _on_tool_result(self, tool_name: str, result: Dict[str, Any]) -> None:
        tool_id = getattr(self, "_last_tool_id", f"t_{uuid.uuid4().hex[:12]}")
        ok = bool(result.get("success", True))
        error = result.get("error") if not ok else None
        preview = _result_preview(result)
        self.emit(
            "tool_result",
            id=tool_id,
            ok=ok,
            preview=preview,
            fullAvailable=len(str(result)) > len(preview),
            error=error,
        )

    def _on_agent_lifecycle(self, action: str, info: AgentInfo) -> None:
        self.emit(
            "agent_lifecycle",
            action=action,
            agent=_agent_info_dict(info),
        )

    # -- inbound (stdin) ------------------------------------------------------

    async def _read_commands(self) -> None:
        """Read NDJSON commands from stdin and dispatch them."""
        loop = asyncio.get_running_loop()

        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        try:
            await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        except Exception as e:
            logger.error("Failed to hook stdin: %s", e)
            return

        while not self._exit.is_set():
            try:
                line = await reader.readline()
            except Exception as e:
                logger.error("stdin read failed: %s", e)
                break
            if not line:
                # EOF — the UI closed the pipe; shut down gracefully.
                self._exit.set()
                break
            try:
                msg = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if msg.get("kind") != "cmd":
                continue
            await self._dispatch(msg)

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
            self.emit("error", category="internal",
                      message=f"{cmd} failed: {e}")

    # -- main loop ------------------------------------------------------------

    async def run(self) -> None:
        """Run until the UI exits or stdin closes."""
        self.emit_hello()
        self.emit_ready()
        self._said_goodbye = False
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


# --- Argument / result sanitization -----------------------------------------

_ARG_PREVIEW_LIMIT = 240
_RESULT_PREVIEW_LIMIT = 400


def _redact_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """Truncate absurdly long arg values so the UI stays snappy."""
    if not isinstance(args, dict):
        return {"value": str(args)[:_ARG_PREVIEW_LIMIT]}
    out: Dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > _ARG_PREVIEW_LIMIT:
            out[k] = v[:_ARG_PREVIEW_LIMIT] + "…"
        else:
            out[k] = v
    return out


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


# --- Command handlers -------------------------------------------------------

async def _cmd_send_message(server: IPCServer, msg: Dict[str, Any]) -> None:
    text = msg.get("text", "")
    server.emit("assistant_start")
    try:
        response = await server.agent.process_message(text)
        content = response.get("content") or ""
        server.emit("assistant_end", content=content)
    except Exception as e:
        server.emit(
            "error",
            category="internal",
            message=str(e),
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
    server.agent.model = model
    try:
        server.agent.provider = server.agent._create_provider()
    except Exception as e:
        server.agent.model = old_model
        server.emit("error", category="provider",
                    message=f"Could not switch to {model}: {e}")
        return
    server.emit("model_changed", model=model,
                provider=server.agent.provider.__class__.__name__)
    server.emit("success", message=f"Model → {model}")


async def _cmd_toggle_auto_approve(server: IPCServer, msg: Dict[str, Any]) -> None:
    server.agent.auto_approve = not server.agent.auto_approve
    state = "ON" if server.agent.auto_approve else "OFF"
    # Emit a structured event so the UI can update its "safe"/"YOLO" badge
    # synchronously, in addition to the user-visible toast.
    server.emit("auto_approve_changed", autoApprove=bool(server.agent.auto_approve))
    server.emit("info", message=f"Auto-approve is now {state}")


async def _cmd_set_reasoning(server: IPCServer, msg: Dict[str, Any]) -> None:
    effort = str(msg.get("effort", "none")).lower()
    if effort not in ("high", "medium", "low", "none"):
        server.emit(
            "warning",
            message=f"Invalid reasoning effort: {effort!r}. Use high|medium|low|none.",
        )
        return
    # The agent may or may not have a reasoning attribute depending on the
    # active provider; set it if present, otherwise just acknowledge.
    try:
        setattr(server.agent, "reasoning_effort", effort)
    except Exception:
        pass
    server.emit("reasoning_changed", effort=effort)
    server.emit("info", message=f"Reasoning effort → {effort}")


async def _cmd_tool_approval_resp(server: IPCServer, msg: Dict[str, Any]) -> None:
    tool_id = str(msg.get("toolId") or msg.get("id") or "")
    approve = bool(msg.get("approve", False))
    waiter = server._approval_waiters.pop(tool_id, None)
    if waiter is not None and not waiter.done():
        waiter.set_result(approve)


async def _cmd_clear_context(server: IPCServer, msg: Dict[str, Any]) -> None:
    server.agent.session = None
    server.agent.create_session()
    server.agent.context_manager.clear()
    server.agent.total_prompt_tokens = 0
    server.agent.total_completion_tokens = 0
    server.agent.total_tokens = 0
    server.emit("success", message="Context cleared")
    server.emit_status()


async def _cmd_compact_context(server: IPCServer, msg: Dict[str, Any]) -> None:
    try:
        await server.agent.compact_context()
        server.emit("success", message="Context compacted")
    except Exception as e:
        server.emit("error", category="internal",
                    message=f"Compaction failed: {e}")
    server.emit_status()


async def _cmd_get_state(server: IPCServer, msg: Dict[str, Any]) -> None:
    server.emit_status()
    for info in agent_tracker.get_all():
        server.emit("agent_update", agent=_agent_info_dict(info))


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
    "exit": _cmd_exit,
}
