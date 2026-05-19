"""Map agent events to session + timeline state."""

from __future__ import annotations

import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from .state import AgentInfo, SessionState
from .timeline_append import append_capped

STREAM_FLUSH_S = 0.120
STATUS_THROTTLE_S = 0.250
FINISHED = frozenset({"done", "error", "cancelled"})


class EventReducer:
    """Stateful reducer for agent → UI events."""

    def __init__(self) -> None:
        self.session = SessionState()
        self.timeline: List[Dict[str, Any]] = []
        self._id_counter = 0
        self._current_assistant_id: Optional[str] = None
        self._stream_pending_content = ""
        self._stream_pending_reasoning = ""
        self._stream_flush_at: Optional[float] = None
        self._status_pending: Optional[Dict[str, Any]] = None
        self._status_flush_at: Optional[float] = None
        self._awaiting_first_delta = False
        self._goodbye = False
        self.on_change: Optional[Callable[[], None]] = None

    def next_id(self) -> str:
        self._id_counter += 1
        return f"t_{self._id_counter}_{uuid.uuid4().hex[:8]}"

    def _notify(self) -> None:
        if self.on_change:
            self.on_change()

    def _push(self, item: Dict[str, Any]) -> None:
        self.timeline = append_capped(self.timeline, item, self.next_id)
        self._notify()

    def _flush_stream_buffers(self) -> None:
        add_c = self._stream_pending_content
        add_r = self._stream_pending_reasoning
        if not add_c and not add_r:
            return
        self._stream_pending_content = ""
        self._stream_pending_reasoning = ""
        aid = self._current_assistant_id
        if not aid:
            return
        for i in range(len(self.timeline) - 1, -1, -1):
            it = self.timeline[i]
            if it.get("id") == aid and it.get("kind") == "assistant":
                it["content"] = it.get("content", "") + add_c
                it["reasoning"] = it.get("reasoning", "") + add_r
                self._notify()
                return

    def _maybe_flush_stream(self) -> None:
        now = time.monotonic()
        if self._stream_flush_at is None:
            self._stream_flush_at = now + STREAM_FLUSH_S
            return
        if now >= self._stream_flush_at:
            self._stream_flush_at = None
            self._flush_stream_buffers()

    def _reset_stream(self) -> None:
        self._stream_pending_content = ""
        self._stream_pending_reasoning = ""
        self._stream_flush_at = None

    def _recover_incomplete_turn(self) -> None:
        self._reset_stream()
        self._current_assistant_id = None
        self.session.thinking = False
        self.session.streaming = False
        for it in self.timeline:
            if it.get("kind") == "assistant" and it.get("streaming"):
                it["streaming"] = False
        self._notify()

    def _apply_status(self, data: Dict[str, Any]) -> None:
        self.session.ctx_used = int(data.get("ctxUsed") or 0)
        self.session.ctx_limit = int(data.get("ctxLimit") or 0)
        self.session.cost_usd = float(data.get("costUsd") or 0)
        self.session.budget_usd = float(data.get("budgetUsd") or 0)
        self.session.prompt_tokens = int(data.get("promptTokens") or 0)
        self.session.completion_tokens = int(data.get("completionTokens") or 0)
        self._notify()

    def handle(self, event: str, data: Dict[str, Any]) -> None:
        if event == "hello":
            self.session.connected = True
            self.session.model = str(data.get("model", ""))
            self.session.provider = str(data.get("provider", ""))
            self.session.cwd = str(data.get("cwd", ""))
            self.session.version = str(data.get("version", ""))
            self.session.ctx_limit = int(data.get("contextLimit") or 0)
            self.session.budget_usd = float(data.get("budgetLimit") or 0)
            self.session.auto_approve = bool(data.get("autoApprove"))
            self.session.reasoning = data.get("reasoning") or "none"
            if self.session.session_started_at is None:
                self.session.session_started_at = time.time()
        elif event == "ready":
            self.session.ready = True
            self._recover_incomplete_turn()
        elif event == "turn":
            phase = data.get("phase")
            if phase == "start":
                self._reset_stream()
                self._awaiting_first_delta = True
                self.session.thinking = True
                self.session.streaming = False
                self.session.progress = None
                item = {
                    "kind": "assistant",
                    "id": self.next_id(),
                    "content": "",
                    "streaming": True,
                    "reasoning": "",
                }
                self._current_assistant_id = item["id"]
                self.timeline = append_capped(self.timeline, item, self.next_id)
            elif phase in ("reasoning", "text") and data.get("delta"):
                if self._awaiting_first_delta:
                    self._awaiting_first_delta = False
                    self.session.thinking = False
                    self.session.streaming = True
                if phase == "reasoning":
                    self._stream_pending_reasoning += str(data["delta"])
                else:
                    self._stream_pending_content += str(data["delta"])
                self._maybe_flush_stream()
            elif phase == "end":
                self._awaiting_first_delta = False
                self._flush_stream_buffers()
                pending_c = self._stream_pending_content
                pending_r = self._stream_pending_reasoning
                self._stream_pending_content = ""
                self._stream_pending_reasoning = ""
                aid = self._current_assistant_id
                if aid:
                    for i in range(len(self.timeline) - 1, -1, -1):
                        it = self.timeline[i]
                        if it.get("id") == aid and it.get("kind") == "assistant":
                            merged_c = it.get("content", "") + pending_c
                            merged_r = it.get("reasoning", "") + pending_r
                            if not merged_c.strip() and not merged_r.strip():
                                self.timeline.pop(i)
                            else:
                                it["content"] = merged_c
                                it["reasoning"] = merged_r
                                it["streaming"] = False
                            break
                self._current_assistant_id = None
                self.session.streaming = False
                self.session.thinking = False
        elif event == "tool":
            tid = str(data.get("id", ""))
            phase = data.get("phase")
            payload = data.get("payload") or {}
            if phase in ("queued", "running"):
                if not any(it.get("kind") == "tool" and it.get("id") == tid for it in self.timeline):
                    if payload.get("name"):
                        self._push(
                            {
                                "kind": "tool",
                                "id": tid,
                                "name": payload["name"],
                                "category": payload.get("category") or "other",
                                "args": payload.get("args") or {},
                                "risk": payload.get("risk") or "low",
                                "ok": None,
                                "preview": None,
                                "error": None,
                                "full_available": False,
                            }
                        )
            elif phase in ("ok", "err"):
                for it in self.timeline:
                    if it.get("kind") == "tool" and it.get("id") == tid:
                        it["ok"] = phase == "ok"
                        it["preview"] = payload.get("preview") or ""
                        it["error"] = payload.get("error")
                        it["full_available"] = bool(payload.get("fullAvailable"))
                        break
            elif phase == "awaiting_approval":
                self._push(
                    {
                        "kind": "approval",
                        "id": tid,
                        "tool": payload.get("name", ""),
                        "args": payload.get("args") or {},
                        "risk": payload.get("risk") or "low",
                        "decided": "pending",
                        "diff": payload.get("diff"),
                    }
                )
            elif phase == "cancelled":
                reason = payload.get("reason")
                if reason is None and payload.get("timeoutSeconds") is not None:
                    reason = f"timed out after {payload['timeoutSeconds']}s"
                reason = reason or "cancelled"
                for it in self.timeline:
                    if it.get("kind") == "approval" and it.get("id") == tid and it.get("decided") == "pending":
                        it["decided"] = "denied"
                    if it.get("kind") == "tool" and it.get("id") == tid and it.get("ok") is None:
                        it["ok"] = False
                        it["error"] = reason
                        it["preview"] = None
        elif event == "agent":
            info = AgentInfo.from_payload(data.get("info") or {})
            prev = self.session.agents.get(info.id)
            became_finished = info.status in FINISHED and (
                prev is None or prev.status not in FINISHED
            )
            if became_finished:
                self.session.agents_finished_at[info.id] = time.time()
            elif info.status not in FINISHED:
                self.session.agents_finished_at.pop(info.id, None)
            self.session.agents[info.id] = info
        elif event == "available_models":
            self.session.available_models = data.get("models")
        elif event == "available_personas":
            self.session.available_personas = data.get("personas")
        elif event == "available_skills":
            self.session.available_skills = data.get("skills")
        elif event == "context_state":
            self.session.context_files = data.get("files")
        elif event == "session_patch":
            if data.get("model") is not None:
                self.session.model = str(data["model"])
            if data.get("provider") is not None:
                self.session.provider = str(data["provider"])
            if data.get("autoApprove") is not None:
                self.session.auto_approve = bool(data["autoApprove"])
            if data.get("reasoning") is not None:
                self.session.reasoning = data["reasoning"]
        elif event == "file_diff":
            self._push(
                {
                    "kind": "diff",
                    "id": self.next_id(),
                    "path": str(data.get("path", "")),
                    "diff": str(data.get("diff", "")),
                }
            )
        elif event in ("info", "warning", "success"):
            self._push(
                {
                    "kind": "toast",
                    "id": self.next_id(),
                    "level": event,
                    "message": str(data.get("message", "")),
                }
            )
        elif event == "status":
            self._status_pending = data
            now = time.monotonic()
            if self._status_flush_at is None:
                self._apply_status(data)
                self._status_flush_at = now + STATUS_THROTTLE_S
            elif now >= self._status_flush_at:
                self._status_flush_at = now + STATUS_THROTTLE_S
                if self._status_pending:
                    self._apply_status(self._status_pending)
        elif event == "error":
            self._recover_incomplete_turn()
            self._push(
                {
                    "kind": "error",
                    "id": self.next_id(),
                    "category": data.get("category", "internal"),
                    "message": str(data.get("message", "")),
                    "hint": data.get("hint"),
                    "details": data.get("details"),
                }
            )
        elif event == "progress":
            self.session.progress = {
                "label": data.get("label", ""),
                "current": data.get("current"),
                "total": data.get("total"),
                "kind": data.get("progressKind", "steps"),
            }
        elif event == "goodbye":
            self._recover_incomplete_turn()
            self._goodbye = True
            self.session.connected = False
            self._push(
                {
                    "kind": "toast",
                    "id": self.next_id(),
                    "level": "info",
                    "message": "Agent session ended. Type /exit or Ctrl+C twice to close.",
                }
            )
        self._notify()

    def pending_approval(self) -> Optional[Dict[str, Any]]:
        for it in reversed(self.timeline):
            if it.get("kind") == "approval" and it.get("decided") == "pending":
                return it
        return None
