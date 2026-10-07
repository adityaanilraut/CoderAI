"""Continuable sub-agent control plane (list / send / interrupt) over SubAgentManager and TaskSupervisor."""

from __future__ import annotations

import asyncio
import builtins
import time
import inspect
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from contextvars import ContextVar

from coderai.orchestration import DEFAULT_MAX_CONTINUABLE_AGENTS, TERMINAL_AGENT_STATUSES
from coderai.subagents.builder import SubAgentSpec
from coderai.subagents.output import SubAgentResult

if TYPE_CHECKING:
    from coderai.subagents.runner import SubAgentManager

# Default per-session cap on live continuable children (configurable via
# CODERAI_MAX_CONTINUABLE_AGENTS_PER_SESSION or settings orchestration.maxContinuableAgents).
MAX_CONTINUABLE_AGENTS_PER_SESSION = DEFAULT_MAX_CONTINUABLE_AGENTS

# Parent-session notice sinks: the CLI SessionManager registers an appender so
# background settlement notices (harness `subagent-settled` / job completion
# notices) land in the live parent conversation. Missing sink = notice dropped.
_notice_sinks: dict[str, Any] = {}
current_subagent_abort: ContextVar[asyncio.Event | None] = ContextVar(
    "subagent_abort", default=None
)
current_subagent_checkpoint: ContextVar[dict[str, Any] | None] = ContextVar(
    "subagent_checkpoint", default=None
)


def register_session_notice_sink(session_id: str, sink: Any) -> None:
    """Register an appender callable ``sink(session_id, text)`` for a session."""
    if session_id:
        _notice_sinks[session_id] = sink


def unregister_session_notice_sink(session_id: str) -> None:
    _notice_sinks.pop(session_id, None)


def notify_parent_session(session_id: str | None, text: str) -> bool:
    """Deliver a background notice into the parent session when a sink exists."""
    if not session_id:
        return False
    sink = _notice_sinks.get(session_id)
    if sink is None:
        return False
    try:
        result = sink(session_id, text)
        if inspect.isawaitable(result):
            try:
                asyncio.get_running_loop()
                task = asyncio.ensure_future(result)
            except RuntimeError:
                if inspect.iscoroutine(result):
                    result.close()
                return False
            task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        return True
    except Exception:
        return False


def append_parent_session_notice(
    project_root: str,
    session_id: str | None,
    text: str,
    source: str = "subagent-settled",
) -> bool:
    """Durably append a parent-facing notice as a user message (no live sink)."""
    if not session_id:
        return False
    try:
        import uuid
        from coderai.soul.session.store import JsonlSessionStore

        # Match the live notice sink's legacy row contract. Unnumbered message
        # rows avoid minting event sequences outside the owning manager.
        store = JsonlSessionStore(project_root)
        store.append_row(
            session_id,
            {
                "id": uuid.uuid4().hex,
                "session_id": session_id,
                "role": "user",
                "content": text,
                "meta": {"advisoryRole": "user", "source": source},
            },
        )
        return True
    except Exception:
        return False


@dataclass
class AgentHandle:
    id: str
    parent_session_id: str
    description: str
    mode: str
    status: str = "running"  # running | completed | interrupted | failed | timeout
    inbox: deque[str] = field(default_factory=deque)
    result: SubAgentResult | None = None
    task: asyncio.Task[Any] | None = None
    spec: SubAgentSpec | None = None
    report: str | None = None
    parent_agent_id: str | None = None
    root_agent_id: str | None = None
    depth: int = 0
    children_ids: list[str] = field(default_factory=list)
    lifecycle_history: list[dict[str, Any]] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    last_heartbeat_at: float = field(default_factory=time.time)
    # Continuable runtime wiring (set by spawn_background_agent)
    inbox_waiter: asyncio.Event | None = None
    manager: SubAgentManager | None = None
    run_session_id: str | None = None
    parent_notice: Any = None
    settled_notified: bool = False
    last_stop_reason: str | None = None
    conversation: list[dict[str, Any]] | None = None
    killed: bool = False

    def to_public_dict(self) -> dict[str, Any]:
        summary = self.report or (self.result.summary if self.result else None)
        return {
            "id": self.id,
            "description": self.description,
            "mode": self.mode,
            "status": self.status,
            "inbox": len(self.inbox),
            "parent_agent_id": self.parent_agent_id,
            "root_agent_id": self.root_agent_id,
            "depth": self.depth,
            "children_ids": list(self.children_ids),
            "started_at": self.started_at,
            "last_heartbeat_at": self.last_heartbeat_at,
            "stopReason": self.last_stop_reason,
            "report": self.report,
            "summary": summary,
        }


class AgentRegistry:
    """Live agent state owned by one event-loop thread; callers marshal mutations there."""

    def __init__(self) -> None:
        self._agents: dict[str, AgentHandle] = {}
        self._watchers: set[asyncio.Event] = set()
        self._owner_thread = threading.get_ident()

    def _check_owner(self) -> None:
        if threading.get_ident() != self._owner_thread:
            raise RuntimeError("Agent registry mutations require the owning event-loop thread")

    def watch(self, event: asyncio.Event) -> None:
        self._check_owner()
        self._watchers.add(event)

    def unwatch(self, event: asyncio.Event) -> None:
        self._check_owner()
        self._watchers.discard(event)

    def changed(self) -> None:
        self._check_owner()
        for event in tuple(self._watchers):
            event.set()

    def register(self, handle: AgentHandle) -> AgentHandle:
        self._check_owner()
        if handle.id in self._agents:
            raise ValueError(f"Agent '{handle.id}' is already registered")
        self._agents[handle.id] = handle
        self.changed()
        return handle

    def get(self, agent_id: str) -> AgentHandle | None:
        return self._agents.get(agent_id)

    def list(self, parent_session_id: str | None = None) -> builtins.list[AgentHandle]:
        items = list(self._agents.values())
        if parent_session_id:
            items = [a for a in items if a.parent_session_id == parent_session_id]
        return items

    def get_children(self, agent_id: str) -> builtins.list[AgentHandle]:
        """Return direct child agents of the given agent."""
        parent = self._agents.get(agent_id)
        if not parent:
            return []
        return [self._agents[cid] for cid in parent.children_ids if cid in self._agents]

    def list_descendants(self, root_id: str) -> builtins.list[AgentHandle]:
        """Stable pre-order walk of the complete tree below ``root_id``."""
        out: builtins.list[AgentHandle] = []

        seen = {root_id}
        stack = list(reversed(self.get_children(root_id)))
        while stack:
            child = stack.pop()
            if child.id in seen:
                continue
            seen.add(child.id)
            out.append(child)
            stack.extend(reversed(self.get_children(child.id)))
        return out

    def get_tree(self, agent_id: str) -> dict[str, Any] | None:
        """Return recursive tree dict of agent and all its descendants."""
        agent = self._agents.get(agent_id)
        if not agent:
            return None

        def _build_node(handle: AgentHandle) -> dict[str, Any]:
            return {
                "id": handle.id,
                "description": handle.description,
                "mode": handle.mode,
                "status": handle.status,
                "depth": handle.depth,
                "parent_agent_id": handle.parent_agent_id,
                "root_agent_id": handle.root_agent_id,
                "children": [],
            }

        root = _build_node(agent)
        seen = {agent_id}
        pending = [(agent, root)]
        while pending:
            handle, node = pending.pop()
            for child in self.get_children(handle.id):
                if child.id in seen:
                    continue
                seen.add(child.id)
                child_node = _build_node(child)
                node["children"].append(child_node)
                pending.append((child, child_node))
        return root

    def send(self, agent_id: str, message: str) -> AgentHandle | None:
        self._check_owner()
        handle = self._agents.get(agent_id)
        if handle is None:
            return None
        if handle.killed:
            raise ValueError("Cannot send to a killed agent")
        if not isinstance(message, str) or len(message.encode("utf-8")) > 65536:
            raise ValueError("Agent messages must be text of at most 64 KiB")
        if len(handle.inbox) >= 100:
            raise ValueError("Agent inbox is full")
        handle.inbox.append(message)
        # Wake a parked continuable worker; its FIFO inbox becomes its next turn.
        if handle.inbox_waiter is not None:
            handle.inbox_waiter.set()
        if handle.status in TERMINAL_AGENT_STATUSES and handle.task and not handle.task.done():
            handle.status = "running"
            handle.result = None
            handle.report = None
            handle.last_stop_reason = None
            handle.settled_notified = False
        self.changed()
        return handle

    def interrupt(self, agent_id: str) -> AgentHandle | None:
        """Stop the agent's CURRENT turn while keeping it available for follow-ups.

        Mirrors the harness ``interrupt_agent`` semantics: only the current
        turn stops; queued messages stay parked until a later ``send``; the
        agent itself remains live. Handles without continuable wiring fall
        back to full task cancellation (legacy behavior).
        """
        self._check_owner()
        handle = self._agents.get(agent_id)
        if handle is None:
            return None
        handle.status = "interrupted"
        self.changed()
        manager = getattr(handle, "manager", None)
        session_id = getattr(handle, "run_session_id", None)
        if manager is not None and session_id:
            # Abort the in-flight turn; the parked loop keeps the inbox.
            manager.cancel_subagent(session_id)
            return handle
        if handle.task and not handle.task.done():
            handle.task.cancel()
        return handle

    def interrupt_tree(self, agent_id: str) -> builtins.list[str]:
        """Recursively cancel an agent and all its child subagents."""
        targets = [agent_id] + [h.id for h in self.list_descendants(agent_id)]
        return [aid for aid in targets if self.interrupt(aid) is not None]

    def kill(self, agent_id: str) -> AgentHandle | None:
        """Permanently terminate a subagent worker and cancel its task."""
        self._check_owner()
        handle = self._agents.get(agent_id)
        if handle is None:
            return None
        handle.killed = True
        handle.status = "interrupted"
        self.changed()
        manager = getattr(handle, "manager", None)
        session_id = getattr(handle, "run_session_id", None)
        if manager is not None and session_id:
            manager.cancel_subagent(session_id)
        if handle.inbox_waiter is not None:
            handle.inbox_waiter.set()
        # Native workers race the abort signal against provider/tool awaits
        # and return an interrupted checkpoint. Cancelling their invoking task
        # as well would also cancel a foreground caller's parent turn.
        if (manager is None or not session_id) and handle.task and not handle.task.done():
            handle.task.cancel()
        return handle

    def evict(self, agent_id: str) -> bool:
        """Remove handle from registry."""
        self._check_owner()
        handle = self._agents.get(agent_id)
        if handle is None:
            return False
        if handle.task is not None and not handle.task.done():
            raise ValueError("Cannot evict a live agent; stop and await its owned worker first")
        self._agents.pop(agent_id)
        parent = self._agents.get(handle.parent_agent_id) if handle.parent_agent_id else None
        if parent is not None:
            parent.children_ids[:] = [
                child_id for child_id in parent.children_ids if child_id != agent_id
            ]
        self.changed()
        return True

    def evict_terminal(self) -> builtins.list[str]:
        """Evict handles that are finished and whose tasks are completed."""
        to_evict = [
            aid
            for aid, h in self._agents.items()
            if (h.killed or h.status in TERMINAL_AGENT_STATUSES)
            and (h.task is None or h.task.done())
        ]
        for aid in to_evict:
            self.evict(aid)
        return to_evict


_registry = AgentRegistry()


def get_agent_registry() -> AgentRegistry:
    return _registry


import uuid

from coderai.utils.common.usage import extract_usage_dict


def _normalize_subagent_tool_calls(raw: Any) -> list[dict[str, Any]] | None:
    if not raw:
        return None
    result: list[dict[str, Any]] = []
    for tc in raw:
        if isinstance(tc, dict):
            func = tc.get("function") or {}
            tc_id = tc.get("id") or uuid.uuid4().hex
            result.append(
                {
                    "id": tc_id,
                    "type": "function",
                    "function": {
                        "name": func.get("name", ""),
                        "arguments": func.get("arguments", "") or "",
                    },
                }
            )
        else:
            tc_id = getattr(tc, "id", "") or uuid.uuid4().hex
            result.append(
                {
                    "id": tc_id,
                    "type": "function",
                    "function": {
                        "name": getattr(getattr(tc, "function", None), "name", "") or "",
                        "arguments": getattr(getattr(tc, "function", None), "arguments", "") or "",
                    },
                }
            )
    return result or None


def _call_llm_sync(client: Any, request: dict[str, Any]) -> dict[str, Any]:
    """Synchronous LLM call wrapper supporting both OpenAI SDK objects and dict responses."""
    from coderai.utils.common.openai_thinking import (
        extract_reasoning_content,
        reasoning_key_for_model,
    )

    # The response reasoning field follows the provider's
    # reasoning_key (normalized back to "reasoning_content" downstream).
    reasoning_key = reasoning_key_for_model(str(request.get("model") or ""))
    abort = current_subagent_abort.get()
    if abort is not None and abort.is_set():
        raise asyncio.CancelledError()
    try:
        resp = client.chat.completions.create(**request)
    except Exception as error:
        # Some OpenAI-compatible endpoints stream tokens but reject usage
        # options. Negotiate that capability once before the turn starts.
        if (
            getattr(error, "status_code", None) not in (None, 400, 422)
            or "stream_options" not in str(error).lower()
            or "stream_options" not in request
        ):
            raise
        if abort is not None and abort.is_set():
            raise asyncio.CancelledError()
        retry = dict(request)
        retry.pop("stream_options", None)
        resp = client.chat.completions.create(**retry)
    from coderai.wire.emitter import get_emitter

    emitter = get_emitter()
    checkpoint = current_subagent_checkpoint.get()

    def text_chunk(text: str) -> None:
        if abort is not None and abort.is_set():
            return
        if checkpoint is not None:
            checkpoint["streaming_content"] = checkpoint.get("streaming_content", "") + text
        emitter.text(text)

    def thinking_chunk(text: str) -> None:
        if abort is not None and abort.is_set():
            return
        if checkpoint is not None:
            checkpoint["streaming_thinking"] = checkpoint.get("streaming_thinking", "") + text
        emitter.think(text)

    if not isinstance(resp, dict) and not hasattr(resp, "choices"):
        from coderai.soul.session.streaming import assemble_stream_response

        finish_reason: str | None = None

        def chunks():
            nonlocal finish_reason
            for chunk in resp:
                choices = getattr(chunk, "choices", None) or []
                if choices:
                    finish_reason = getattr(choices[0], "finish_reason", None) or finish_reason
                yield chunk

        abort = current_subagent_abort.get()
        assembled = assemble_stream_response(
            chunks(),
            reasoning_key,
            on_chunk=text_chunk,
            on_thinking_chunk=thinking_chunk,
            is_cancelled=abort.is_set if abort is not None else None,
            close_stream=getattr(resp, "close", None),
        )
        assembled["choices"][0]["finish_reason"] = finish_reason
        return assembled
    if isinstance(resp, dict):
        choices = resp.get("choices") or [{}]
        choice = choices[0] if choices else {}
        msg = choice.get("message") or {}
        dict_res: dict[str, Any] = {
            "choices": [
                {
                    "finish_reason": choice.get("finish_reason"),
                    "message": {
                        "content": msg.get("content") or "",
                        "tool_calls": msg.get("tool_calls"),
                        "reasoning_content": extract_reasoning_content(msg, reasoning_key),
                        "refusal": msg.get("refusal"),
                    },
                }
            ]
        }
        usage = resp.get("usage")
        if usage:
            dict_res["usage"] = extract_usage_dict(usage)
        if msg.get("content"):
            text_chunk(msg["content"])
        reasoning = extract_reasoning_content(msg, reasoning_key)
        if reasoning:
            thinking_chunk(reasoning)
        return dict_res

    choice = resp.choices[0]
    msg = choice.message
    tool_calls = None
    raw_tc = getattr(msg, "tool_calls", None)
    if raw_tc:
        tool_calls = []
        for tc in raw_tc:
            func = getattr(tc, "function", None)
            tool_calls.append(
                {
                    "id": getattr(tc, "id", "") or uuid.uuid4().hex,
                    "type": "function",
                    "function": {
                        "name": getattr(func, "name", "") or "",
                        "arguments": getattr(func, "arguments", "") or "",
                    },
                }
            )

    res: dict[str, Any] = {
        "choices": [
            {
                "finish_reason": getattr(choice, "finish_reason", None),
                "message": {
                    "content": getattr(msg, "content", None) or "",
                    "tool_calls": tool_calls,
                    "reasoning_content": extract_reasoning_content(msg, reasoning_key),
                    "refusal": getattr(msg, "refusal", None),
                },
            }
        ]
    }
    usage_attr = getattr(resp, "usage", None)
    if usage_attr:
        res["usage"] = extract_usage_dict(usage_attr)
    if getattr(msg, "content", None):
        text_chunk(msg.content)
    reasoning = extract_reasoning_content(msg, reasoning_key)
    if reasoning:
        thinking_chunk(reasoning)
    return res
