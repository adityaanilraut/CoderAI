"""Continuable sub-agent control plane (list / send / interrupt) over SubAgentManager and TaskSupervisor."""

from __future__ import annotations

import asyncio
import builtins
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from coderai.orchestration import DEFAULT_MAX_CONTINUABLE_AGENTS
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
        sink(session_id, text)
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
        from coderai.events import make_user_message
        from coderai.soul.session.store import JsonlSessionStore

        store = JsonlSessionStore(project_root)
        max_seq = 0
        for row in store.read_rows(session_id):
            seq = row.get("seq")
            if isinstance(seq, int):
                max_seq = max(max_seq, seq + 1)
        event = make_user_message(
            max_seq,
            text,
            source="user",
            meta={"advisoryRole": "user", "source": source},
        )
        store.append_row(session_id, event.to_dict())
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
    inbox: list[str] = field(default_factory=list)
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
    def __init__(self) -> None:
        self._agents: dict[str, AgentHandle] = {}

    def register(self, handle: AgentHandle) -> AgentHandle:
        self._agents[handle.id] = handle
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

        def _walk(agent_id: str) -> None:
            for child in self.get_children(agent_id):
                out.append(child)
                _walk(child.id)

        _walk(root_id)
        return out

    def get_tree(self, agent_id: str) -> dict[str, Any] | None:
        """Return recursive tree dict of agent and all its descendants."""
        agent = self._agents.get(agent_id)
        if not agent:
            return None

        def _build_node(handle: AgentHandle) -> dict[str, Any]:
            children_nodes = []
            for cid in handle.children_ids:
                child = self._agents.get(cid)
                if child:
                    children_nodes.append(_build_node(child))
            return {
                "id": handle.id,
                "description": handle.description,
                "mode": handle.mode,
                "status": handle.status,
                "depth": handle.depth,
                "parent_agent_id": handle.parent_agent_id,
                "root_agent_id": handle.root_agent_id,
                "children": children_nodes,
            }

        return _build_node(agent)

    def send(self, agent_id: str, message: str) -> AgentHandle | None:
        handle = self._agents.get(agent_id)
        if handle is None:
            return None
        handle.inbox.append(message)
        # Wake a parked continuable worker; its FIFO inbox becomes its next turn.
        if handle.inbox_waiter is not None:
            handle.inbox_waiter.set()
        if (
            handle.status in ("completed", "interrupted", "timeout", "failed")
            and handle.task
            and not handle.task.done()
        ):
            handle.status = "running"
        return handle

    def interrupt(self, agent_id: str) -> AgentHandle | None:
        """Stop the agent's CURRENT turn while keeping it available for follow-ups.

        Mirrors the harness ``interrupt_agent`` semantics: only the current
        turn stops; queued messages stay parked until a later ``send``; the
        agent itself remains live. Handles without continuable wiring fall
        back to full task cancellation (legacy behavior).
        """
        handle = self._agents.get(agent_id)
        if handle is None:
            return None
        handle.status = "interrupted"
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
        interrupted: builtins.list[str] = []

        def _cancel_rec(aid: str) -> None:
            h = self.interrupt(aid)
            if h is not None:
                interrupted.append(aid)
                for cid in list(h.children_ids):
                    _cancel_rec(cid)

        _cancel_rec(agent_id)
        return interrupted


_registry = AgentRegistry()


def get_agent_registry() -> AgentRegistry:
    return _registry


# --- module helpers (from coderai/core/subagent.py) ---
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
    resp = client.chat.completions.create(**request)
    if isinstance(resp, dict):
        choices = resp.get("choices") or [{}]
        choice = choices[0] if choices else {}
        msg = choice.get("message") or {}
        dict_res: dict[str, Any] = {
            "choices": [
                {
                    "message": {
                        "content": msg.get("content") or "",
                        "tool_calls": msg.get("tool_calls"),
                        "reasoning_content": extract_reasoning_content(msg, reasoning_key),
                        "refusal": msg.get("refusal"),
                    }
                }
            ]
        }
        usage = resp.get("usage")
        if usage:
            dict_res["usage"] = extract_usage_dict(usage)
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
                "message": {
                    "content": getattr(msg, "content", None) or "",
                    "tool_calls": tool_calls,
                    "reasoning_content": extract_reasoning_content(msg, reasoning_key),
                    "refusal": getattr(msg, "refusal", None),
                }
            }
        ]
    }
    usage_attr = getattr(resp, "usage", None)
    if usage_attr:
        res["usage"] = extract_usage_dict(usage_attr)
    return res
