"""Map agent events to session + timeline state."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from coderAI.tui.state import AgentInfo, SessionState
from coderAI.tui.timeline_render import append_capped

STREAM_FLUSH_S = 0.120
STATUS_THROTTLE_S = 0.250

# UI refresh modes (higher priority wins when multiple fire in one handle()).
_REFRESH_PRIORITY = {"chrome": 1, "stream": 2, "append": 3, "full": 4}
RefreshMode = str  # "full" | "append" | "stream" | "chrome"


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
        self.on_change: Optional[Callable[[RefreshMode], None]] = None
        self._pending_refresh: Optional[RefreshMode] = None
        # Highest context-usage threshold already toasted (0, 80, or 90);
        # resets once usage drops back below 75% (e.g. after /compact).
        self._ctx_warned = 0

    def next_id(self) -> str:
        self._id_counter += 1
        return f"t_{self._id_counter}_{uuid.uuid4().hex[:8]}"

    def toast(self, level: str, message: str) -> None:
        """Push a toast notification to the timeline and refresh."""
        self._push({"kind": "toast", "id": self.next_id(), "level": level, "message": message})
        self._bump_refresh("append")
        self._notify()

    def _bump_refresh(self, mode: RefreshMode) -> None:
        if self._pending_refresh is None:
            self._pending_refresh = mode
        elif _REFRESH_PRIORITY[mode] > _REFRESH_PRIORITY[self._pending_refresh]:
            self._pending_refresh = mode

    def _notify(self) -> None:
        if self._pending_refresh and self.on_change:
            self.on_change(self._pending_refresh)
        self._pending_refresh = None

    def _push(self, item: Dict[str, Any]) -> None:
        item.setdefault("ts", time.time())
        item.setdefault("collapsed", False)
        self.timeline = append_capped(self.timeline, item, self.next_id)

    def _flush_stream_buffers(self) -> bool:
        add_c = self._stream_pending_content
        add_r = self._stream_pending_reasoning
        if not add_c and not add_r:
            return False
        self._stream_pending_content = ""
        self._stream_pending_reasoning = ""
        aid = self._current_assistant_id
        if not aid:
            return False
        for i in range(len(self.timeline) - 1, -1, -1):
            it = self.timeline[i]
            if it.get("id") == aid and it.get("kind") == "assistant":
                if add_c:
                    it["content"] = it.get("content", "") + add_c
                if add_r:
                    it["reasoning"] = it.get("reasoning", "") + add_r
                return True
        return False

    def _maybe_flush_stream(self) -> bool:
        now = time.monotonic()
        if self._stream_flush_at is None:
            self._stream_flush_at = now + STREAM_FLUSH_S
            return False
        if now >= self._stream_flush_at:
            self._stream_flush_at = None
            return self._flush_stream_buffers()
        return False

    def _reset_stream(self) -> None:
        self._stream_pending_content = ""
        self._stream_pending_reasoning = ""
        self._stream_flush_at = None

    def _maybe_flush_status(self) -> bool:
        if self._status_pending is None or self._status_flush_at is None:
            return False
        now = time.monotonic()
        if now >= self._status_flush_at:
            self._apply_status(self._status_pending)
            self._status_pending = None
            self._status_flush_at = None
            self._bump_refresh("chrome")
            return True
        return False

    def tick(self) -> None:
        """Drive one coalescing pass: flush stream/status buffers on cadence.

        Called from the app's timer so the app doesn't reach into the
        reducer's flush privates directly.
        """
        flushed_stream = self._maybe_flush_stream()
        # When streaming ends, the time-gate may leave un-flushed content
        # in the buffers. Force one last flush so the user sees final output.
        if not flushed_stream and self._stream_flush_at is None:
            if self._stream_pending_content or self._stream_pending_reasoning:
                flushed_stream = self._flush_stream_buffers()
        if flushed_stream:
            self._bump_refresh("stream")
        flushed_status = self._maybe_flush_status()
        if flushed_stream or flushed_status:
            self._notify()

    def _recover_incomplete_turn(self) -> None:
        self._reset_stream()
        self._current_assistant_id = None
        self.session.thinking = False
        self.session.streaming = False
        for it in self.timeline:
            if it.get("kind") == "assistant" and it.get("streaming"):
                it["streaming"] = False

    @staticmethod
    def _stored_tool_result(content: Any) -> tuple[bool, str, Optional[str]]:
        raw = str(content or "")
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            ok = bool(parsed.get("success", True))
            error = str(parsed.get("error") or "") or None
        else:
            ok = True
            error = None
        return ok, raw[:400], error

    def _replay_session_messages(self, messages: List[Dict[str, Any]]) -> None:
        """Reduce stored API messages into the existing timeline item shapes."""
        for message in messages:
            role = message.get("role")
            timestamp = message.get("timestamp")
            if role == "user":
                self._push(
                    {
                        "kind": "user",
                        "id": self.next_id(),
                        "text": str(message.get("content") or ""),
                        "ts": timestamp,
                    }
                )
                continue
            if role == "assistant":
                content = str(message.get("content") or "")
                reasoning = str(message.get("reasoning_content") or "")
                if content or reasoning:
                    self._push(
                        {
                            "kind": "assistant",
                            "id": self.next_id(),
                            "content": content,
                            "reasoning": reasoning,
                            "streaming": False,
                            "ts": timestamp,
                        }
                    )
                for tool_call in message.get("tool_calls") or []:
                    function = tool_call.get("function") or {}
                    raw_args = function.get("arguments") or "{}"
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except json.JSONDecodeError:
                        args = {}
                    if not isinstance(args, dict):
                        args = {}
                    self._push(
                        {
                            "kind": "tool",
                            "id": str(tool_call.get("id") or self.next_id()),
                            "name": str(function.get("name") or "tool"),
                            "category": "other",
                            "args": args,
                            "risk": "low",
                            "ok": None,
                            "preview": None,
                            "error": None,
                            "full_available": False,
                            "ts": timestamp,
                        }
                    )
                continue
            if role == "tool":
                tool_id = str(message.get("tool_call_id") or self.next_id())
                ok, preview, error = self._stored_tool_result(message.get("content"))
                existing = next(
                    (
                        item
                        for item in reversed(self.timeline)
                        if item.get("kind") == "tool" and item.get("id") == tool_id
                    ),
                    None,
                )
                if existing is None:
                    existing = {
                        "kind": "tool",
                        "id": tool_id,
                        "name": str(message.get("name") or "tool"),
                        "category": "other",
                        "args": {},
                        "risk": "low",
                        "full_available": False,
                        "ts": timestamp,
                    }
                    self._push(existing)
                existing.update({"ok": ok, "preview": preview, "error": error})

    def _apply_status(self, data: Dict[str, Any]) -> None:
        if "workspaceTrusted" in data:
            trusted = data.get("workspaceTrusted")
            self.session.workspace_trusted = None if trusted is None else bool(trusted)
        self.session.ctx_used = int(data.get("ctxUsed") or 0)
        self.session.ctx_limit = int(data.get("ctxLimit") or 0)
        self.session.cost_usd = float(data.get("costUsd") or 0)
        self.session.budget_usd = float(data.get("budgetUsd") or 0)
        self.session.prompt_tokens = int(data.get("promptTokens") or 0)
        self.session.completion_tokens = int(data.get("completionTokens") or 0)
        self.session.iteration = int(data.get("iteration") or 0)
        self.session.max_iterations = int(data.get("maxIterations") or 50)
        self.session.elapsed_s = float(data.get("elapsedSeconds") or 0)
        self._check_ctx_threshold()

    def _check_ctx_threshold(self) -> None:
        """Toast once when context usage crosses 80% / 90% of the limit."""
        limit = self.session.ctx_limit
        if limit <= 0:
            return
        ratio = self.session.ctx_used / limit
        if ratio < 0.75:
            self._ctx_warned = 0
            return
        used = f"{self.session.ctx_used:,}"
        lim = f"{limit:,}"
        if ratio >= 0.9 and self._ctx_warned < 90:
            self._ctx_warned = 90
            self._push(
                {
                    "kind": "toast",
                    "id": self.next_id(),
                    "level": "warning",
                    "message": (
                        f"Context 90% full ({used} / {lim} tokens). "
                        "Run /compact to summarize or /clear to reset."
                    ),
                }
            )
            self._bump_refresh("append")
        elif ratio >= 0.8 and self._ctx_warned < 80:
            self._ctx_warned = 80
            self._push(
                {
                    "kind": "toast",
                    "id": self.next_id(),
                    "level": "info",
                    "message": (
                        f"Context 80% full ({used} / {lim} tokens). "
                        "Consider /compact to free up room."
                    ),
                }
            )
            self._bump_refresh("append")

    def handle(self, event: str, data: Dict[str, Any]) -> None:
        dirty = False
        if event == "hello":
            dirty = True
            self._bump_refresh("full")
            self.session.model = str(data.get("model", ""))
            self.session.provider = str(data.get("provider", ""))
            self.session.cwd = str(data.get("cwd", ""))
            self.session.ctx_limit = int(data.get("contextLimit") or 0)
            self.session.budget_usd = float(data.get("budgetLimit") or 0)
            self.session.auto_approve = bool(data.get("autoApprove"))
            self.session.reasoning = data.get("reasoning") or "none"
            # First hello on an empty timeline seeds the welcome/empty-state
            # block; a re-hello (e.g. after /retry) lands on a populated
            # timeline and skips it.
            if not self.timeline:
                self._push(
                    {
                        "kind": "welcome",
                        "id": self.next_id(),
                        "model": self.session.model,
                        "provider": self.session.provider,
                        "cwd": self.session.cwd,
                    }
                )
        elif event == "ready":
            self.session.ready = True
            self._recover_incomplete_turn()
            dirty = True
            self._bump_refresh("full")
        elif event == "session_replay":
            self._replay_session_messages(data.get("messages") or [])
            dirty = True
            self._bump_refresh("full")
        elif event == "turn":
            phase = data.get("phase")
            if phase == "start":
                dirty = True
                self._bump_refresh("append")
                self._reset_stream()
                self._awaiting_first_delta = True
                self.session.thinking = True
                self.session.streaming = False
                self.session.progress = None
                item: Dict[str, Any] = {
                    "kind": "assistant",
                    "id": self.next_id(),
                    "content": "",
                    "streaming": True,
                    "reasoning": "",
                }
                self._current_assistant_id = item["id"]
                self.timeline = append_capped(self.timeline, item, self.next_id)
            elif phase in ("reasoning", "text") and data.get("delta"):
                session_dirty = False
                if self._awaiting_first_delta:
                    self._awaiting_first_delta = False
                    self.session.thinking = False
                    self.session.streaming = True
                    session_dirty = True
                if phase == "reasoning":
                    self._stream_pending_reasoning += str(data["delta"])
                else:
                    self._stream_pending_content += str(data["delta"])
                if session_dirty:
                    self._flush_stream_buffers()
                    dirty = True
                    self._bump_refresh("stream")
                elif self._maybe_flush_stream():
                    dirty = True
                    self._bump_refresh("stream")
            elif phase == "end":
                dirty = True
                self._bump_refresh("append")
                self._awaiting_first_delta = False
                self._flush_stream_buffers()
                # _flush_stream_buffers already merged pending content;
                # now mark the item non-streaming (or remove if empty).
                aid = self._current_assistant_id
                if aid:
                    for i in range(len(self.timeline) - 1, -1, -1):
                        it = self.timeline[i]
                        if it.get("id") == aid and it.get("kind") == "assistant":
                            if (
                                not it.get("content", "").strip()
                                and not it.get("reasoning", "").strip()
                            ):
                                self.timeline.pop(i)
                            else:
                                it["streaming"] = False
                            break
                self._current_assistant_id = None
                self.session.streaming = False
                self.session.thinking = False
        elif event == "tool":
            dirty = True
            tid = str(data.get("id", ""))
            phase = data.get("phase")
            payload = data.get("payload") or {}
            if phase in ("queued", "running"):
                self._bump_refresh("append")
                if not any(
                    it.get("kind") == "tool" and it.get("id") == tid for it in self.timeline
                ):
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
                self._bump_refresh("full")
                for it in self.timeline:
                    if it.get("kind") == "tool" and it.get("id") == tid:
                        it["ok"] = phase == "ok"
                        it["preview"] = payload.get("preview") or ""
                        it["error"] = payload.get("error")
                        it["full_available"] = bool(payload.get("fullAvailable"))
                        break
            elif phase == "awaiting_approval":
                self._bump_refresh("append")
                self._push(
                    {
                        "kind": "approval",
                        "id": tid,
                        "tool": payload.get("name", ""),
                        "args": payload.get("args") or {},
                        "risk": payload.get("risk") or "low",
                        "riskFactors": payload.get("riskFactors") or [],
                        "decided": "pending",
                        "diff": payload.get("diff"),
                        "requestedBy": payload.get("requestedBy", ""),
                        "parentId": payload.get("parentId"),
                        "iteration": payload.get("iteration", 0),
                        "maxIterations": payload.get("maxIterations", 50),
                        "priorApproved": payload.get("priorApproved", 0),
                        "timeoutSeconds": payload.get("timeoutSeconds", 0),
                        "expiresAt": payload.get("expiresAt"),
                        "rememberMode": payload.get("rememberMode"),
                        "rememberScope": payload.get("rememberScope"),
                        "rememberLabel": payload.get("rememberLabel"),
                    }
                )
            elif phase == "cancelled":
                self._bump_refresh("full")
                reason = payload.get("reason")
                if reason is None and payload.get("timeoutSeconds") is not None:
                    reason = f"timed out after {payload['timeoutSeconds']}s"
                reason = reason or "cancelled"
                for it in self.timeline:
                    if (
                        it.get("kind") == "approval"
                        and it.get("id") == tid
                        and it.get("decided") == "pending"
                    ):
                        it["decided"] = "denied"
                    if it.get("kind") == "tool" and it.get("id") == tid and it.get("ok") is None:
                        it["ok"] = False
                        it["error"] = reason
                        it["preview"] = None
        elif event == "agent":
            dirty = True
            self._bump_refresh("chrome")
            info = AgentInfo.from_payload(data.get("info") or {})
            self.session.agents[info.id] = info
        elif event == "available_models":
            dirty = True
            self._bump_refresh("chrome")
            self.session.available_models = data.get("models")
        elif event == "available_personas":
            dirty = True
            self._bump_refresh("chrome")
            self.session.available_personas = data.get("personas")
        elif event == "available_skills":
            dirty = True
            self._bump_refresh("chrome")
            self.session.available_skills = data.get("skills")
        elif event == "available_mcp_servers":
            dirty = True
            self._bump_refresh("chrome")
            self.session.available_mcp_servers = data.get("servers")
        elif event == "context_state":
            dirty = True
            self._bump_refresh("chrome")
            self.session.context_files = data.get("files")
        elif event == "session_patch":
            dirty = True
            self._bump_refresh("chrome")
            if data.get("model") is not None:
                self.session.model = str(data["model"])
            if data.get("provider") is not None:
                self.session.provider = str(data["provider"])
            if data.get("autoApprove") is not None:
                self.session.auto_approve = bool(data["autoApprove"])
            if data.get("reasoning") is not None:
                self.session.reasoning = data["reasoning"]
            if data.get("persona") is not None:
                self.session.active_persona = data["persona"] or None
            if data.get("verbosity") is not None:
                self.session.verbose = data["verbosity"] == "verbose"
        elif event == "file_diff":
            dirty = True
            self._bump_refresh("append")
            self._push(
                {
                    "kind": "diff",
                    "id": self.next_id(),
                    "path": str(data.get("path", "")),
                    "diff": str(data.get("diff", "")),
                }
            )
        elif event == "tasks_card":
            dirty = True
            self._bump_refresh("chrome")
            self.session.current_tasks = data.get("tasks")
        elif event == "skill_card":
            dirty = True
            self._bump_refresh("append")
            self._push(
                {
                    "kind": "skill_card",
                    "id": data.get("id", self.next_id()),
                    "name": data.get("name", ""),
                    "description": data.get("description", ""),
                    "steps": data.get("steps") or [],
                }
            )
        elif event in ("info", "warning", "success"):
            dirty = True
            self._bump_refresh("append")
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
                dirty = True
                self._bump_refresh("chrome")
            elif now >= self._status_flush_at:
                self._status_flush_at = now + STATUS_THROTTLE_S
                if self._status_pending:
                    self._apply_status(self._status_pending)
                    dirty = True
                    self._bump_refresh("chrome")
        elif event == "error":
            dirty = True
            self._bump_refresh("append")
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
            dirty = True
            self._bump_refresh("chrome")
            self.session.progress = {
                "label": data.get("label", ""),
                "current": data.get("current"),
                "total": data.get("total"),
            }
        elif event == "goodbye":
            dirty = True
            self._bump_refresh("full")
            self._recover_incomplete_turn()
            self._push(
                {
                    "kind": "toast",
                    "id": self.next_id(),
                    "level": "info",
                    "message": "Agent session ended. Type /exit or Ctrl+C twice to close.",
                }
            )
        if dirty:
            self._notify()

    def pending_approval(self) -> Optional[Dict[str, Any]]:
        for it in reversed(self.timeline):
            if it.get("kind") == "approval" and it.get("decided") == "pending":
                return it
        return None
