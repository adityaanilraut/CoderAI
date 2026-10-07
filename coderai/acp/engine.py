"""``SessionManager``-backed engine for the ACP server (Stack A).

:class:`coderai.acp.session.ACPSession` is engine-agnostic: it consumes an async
iterator of wire messages plus a couple of accessors. This module supplies that
contract on top of the live ``SessionManager`` (the full tool platform).

Turn driving mirrors :class:`coderai.wire.server.WireServer`: create/reply the
session, forward process-emitter events, and bridge permission/question pauses
to the client as wire requests.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator, Coroutine
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from coderai.soul.session.manager import SessionManager
    from coderai.wire import WireUISide
    from coderai.wire.emitter import WireEmitter

from kosong.message import ContentPart, ImageURLPart, TextPart

from coderai.utils.logging import logger
from coderai.wire.types import (
    ApprovalRequest,
    QuestionItem,
    QuestionOption,
    QuestionRequest,
    WireMessage,
)

#: Pause classifications returned by :meth:`SessionManagerEngine._classify`.
_END_TURN = "end_turn"
_ASK_PERMISSION = "ask_permission"
_ASK_USER = "ask_user"

_QUESTION_STATUSES = ("ask_user_question", "waiting_for_user")
_APPROVE_RESPONSES = ("approve", "approve_for_session")

#: Mirrors ``coderai.cli.image_attachment.MAX_IMAGE_BYTES``: ACP images arrive
#: as in-memory data URLs, so oversized payloads are dropped with a warning
#: instead of bloating the session store.
MAX_ACP_IMAGE_BYTES = 20 * 1024 * 1024


def _image_content_param(data_url: str) -> dict[str, Any] | None:
    """Build an OpenAI-style ``image_url`` content param from a data URL.

    Returns None (with a warning) for empty payloads or oversized images.
    The param flows into the user message's ``meta["contentParams"]``, which
    :class:`coderai.utils.common.message_converter.OpenAIMessageConverter`
    sends as multimodal input when the model supports it (same path as the
    CLI ``/image`` command) — no temp files, no read-tool roundtrip.
    """
    url = (data_url or "").strip()
    if not url:
        return None
    _header, sep, payload = url.partition(",")
    if sep and payload:
        approx_bytes = (len(payload) * 3) // 4
        if approx_bytes > MAX_ACP_IMAGE_BYTES:
            logger.warning(
                "Dropped oversized ACP image part (%.1fMB > 20MB)",
                approx_bytes / (1024 * 1024),
            )
            return None
    return {"type": "image_url", "image_url": {"url": url}}


def build_acp_prompt(user_input: list[ContentPart]) -> tuple[str, list[dict[str, Any]]]:
    """Split ACP content parts into Stack A prompt text + image params."""
    chunks: list[str] = []
    images: list[dict[str, Any]] = []
    for part in user_input:
        if isinstance(part, TextPart):
            if part.text:
                chunks.append(part.text)
        elif isinstance(part, ImageURLPart):
            param = _image_content_param(part.image_url.url)
            if param is not None:
                images.append(param)
            else:
                logger.warning("Dropped unsupported ACP image part")
        else:
            logger.warning("Dropped unsupported ACP content part: %s", type(part).__name__)
    return "\n".join(chunks).strip(), images


class SessionManagerEngine:
    """Drive one ACP session on the CoderAI ``SessionManager`` engine."""

    def __init__(
        self,
        manager: SessionManager,
        *,
        config: Any = None,
        session: Any = None,
        plan_mode: bool = False,
        skills: list[str] | None = None,
    ) -> None:
        self._manager = manager
        self._config = config
        self._session = session
        self._plan_mode = plan_mode
        self._skills = skills
        self._engine_session_id: str | None = None
        from coderai.wire.emitter import WireEmitter

        self._fallback_emitter = WireEmitter()
        self._running = False
        self._active_task: asyncio.Task | None = None
        self._active_receiver: asyncio.Task | None = None
        self._run_task: asyncio.Task | None = None

    # -- accessors consumed by acp/server.py + acp/session.py ---------------
    @property
    def manager(self) -> SessionManager:
        return self._manager

    @property
    def config(self) -> Any:
        return self._config

    @property
    def session(self) -> Any:
        return self._session

    @property
    def session_id(self) -> str | None:
        """The underlying ``SessionManager`` session id, once a turn has started."""
        return self._engine_session_id

    @property
    def toolset(self) -> None:
        """Stack A exposes tools via ``ToolRegistry``, not a kosong toolset."""
        return None

    @property
    def llm(self) -> None:
        """Models are resolved per turn by ``SessionManager``."""
        return None

    def is_oauth_session(self) -> bool:
        """Best-effort detection of an OAuth-authenticated active provider."""
        try:
            settings = self._manager.get_resolved_settings()
        except Exception:
            return False
        try:
            models = settings.get("models") or {}
            providers = settings.get("providers") or {}
            spec = models.get(self._manager.get_active_model())
            provider = spec.get("provider") if isinstance(spec, dict) else None
            entry = providers.get(provider) if provider else None
            return bool(isinstance(entry, dict) and entry.get("oauth"))
        except Exception:
            return False

    def set_model(self, model_key: str) -> None:
        self._manager.set_model(model_key)

    def set_thinking(self, enabled: bool) -> None:
        """Session-scoped thinking-mode override (Stack A ``thinkingEnabled``).

        Tolerates managers without the setter (test fakes) — the model
        override still applies.
        """
        setter = getattr(self._manager, "set_thinking_enabled", None)
        if callable(setter):
            setter(enabled)

    def set_mode(self, mode: str) -> None:
        """Map ACP's four modes to the existing plan, YOLO and AFK controls."""
        if mode not in {"default", "plan", "auto", "yolo"}:
            raise ValueError("Unknown ACP mode")
        # Hydrate persisted state before applying explicit overrides, otherwise
        # a later resume read could re-enable a mode the client just disabled.
        if self._engine_session_id is not None:
            self._manager.get_session_state(self._engine_session_id)
        self._manager.set_yolo(mode == "yolo")
        self._manager.set_afk(mode == "auto")
        self._plan_mode = mode == "plan"
        if self._engine_session_id is not None:
            self._manager.set_plan_mode(self._engine_session_id, self._plan_mode)

    def bind_session(self, session_id: str) -> None:
        """Attach to an existing ``SessionManager`` session (resume/fork)."""
        self._engine_session_id = session_id

    def interrupt(self) -> None:
        """Ask the engine to stop the in-flight turn."""
        if self._engine_session_id is None:
            return
        with contextlib.suppress(Exception):
            self._manager.interrupt_session(self._engine_session_id)
        if self._active_task is not None:
            self._active_task.cancel()
        if self._run_task is not None and self._run_task is not asyncio.current_task():
            self._run_task.cancel()

    async def close(self) -> None:
        """Settle the active driver before releasing owned processes and MCP."""
        self.interrupt()
        task = self._run_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        from coderai.cli.session_factory import close_session_manager

        await close_session_manager(self._manager)

    def _event_emitter(self) -> WireEmitter:
        getter = getattr(self._manager, "get_event_emitter", None)
        if callable(getter) and self._engine_session_id is not None:
            return getter(self._engine_session_id)
        return self._fallback_emitter

    # -- turn driving --------------------------------------------------------
    async def run(
        self, user_input: list[ContentPart], cancel_event: asyncio.Event
    ) -> AsyncIterator[WireMessage]:
        """Run one prompt, yielding wire messages until the turn settles.

        Runs the prompt and yields wire messages until the turn settles.
        """
        from coderai.wire.emitter import bind_emitter, reset_emitter
        from coderai.soul import RunCancelled

        if self._running:
            raise RuntimeError("Session already has an active turn")
        if cancel_event.is_set():
            raise RunCancelled()
        self._running = True
        new_session = self._engine_session_id is None
        if new_session and callable(getattr(self._manager, "get_event_emitter", None)):
            self._engine_session_id = uuid.uuid4().hex
        emitter = self._event_emitter()
        token = bind_emitter(emitter)
        ui_side = emitter.ui_side(merge=False, replay=False)

        async def _watch_cancel() -> None:
            try:
                await cancel_event.wait()
            except asyncio.CancelledError:
                return
            self.interrupt()
            # Permission/question waits belong to this turn too.
            if self._run_task is not None:
                self._run_task.cancel()

        self._run_task = asyncio.current_task()
        cancel_task = asyncio.create_task(_watch_cancel())
        pending_requests: list[Any] = []
        try:
            text, content_params = build_acp_prompt(user_input)
            async for message in self._drive(
                self._start_turn(text, content_params or None, new_session=new_session), ui_side
            ):
                yield message

            while True:
                session_id = self._engine_session_id
                if session_id is None:
                    return
                status = self._classify() if not cancel_event.is_set() else _END_TURN
                if status == _END_TURN:
                    return
                if status == _ASK_PERMISSION:
                    items = self._pending_approvals()
                    if not items:
                        return
                    entry = self._manager.get_session(session_id)
                    requests = [self._approval_request(item) for item in items]
                    if getattr(entry, "plan_mode", False):
                        for request in requests:
                            request.allow_session_approve = False
                    pending_requests.extend(requests)
                    for request in requests:
                        yield request
                    replies = await self._collect_permission_replies(requests, items)
                    async for message in self._drive(
                        self._manager.respond_permissions(session_id, replies),
                        ui_side,
                    ):
                        yield message
                    continue
                if status == _ASK_USER:
                    questions = self._collect_questions()
                    if not questions:
                        return
                    question_request = self._question_request(questions)
                    pending_requests.append(question_request)
                    yield question_request
                    try:
                        answers = await question_request.wait()
                    except Exception:
                        answers = {}
                    answers = dict(answers) if isinstance(answers, dict) else {}
                    prompt = self._format_answers(questions, answers)
                    async for message in self._drive(
                        self._manager.reply_session(session_id, user_prompt=prompt),
                        ui_side,
                    ):
                        yield message
                    continue
                return
        except asyncio.CancelledError:
            if cancel_event.is_set():
                raise RunCancelled() from None
            self.interrupt()
            raise
        finally:
            for request in pending_requests:
                with contextlib.suppress(Exception):
                    request.resolve("reject" if isinstance(request, ApprovalRequest) else {})
            cancel_task.cancel()
            with contextlib.suppress(BaseException):
                await cancel_task
            for task in (self._active_task, self._active_receiver):
                if task is not None and not task.done():
                    task.cancel()
                    with contextlib.suppress(BaseException):
                        await task
            self._active_task = self._active_receiver = None
            ui_side.close()
            reset_emitter(token)
            self._run_task = None
            self._running = False
            with contextlib.suppress(Exception):
                if (
                    new_session
                    and self._engine_session_id is not None
                    and self._manager.get_session(self._engine_session_id) is None
                ):
                    self._engine_session_id = None

    async def _drive(
        self, coro: Coroutine[Any, Any, Any], ui_side: WireUISide
    ) -> AsyncIterator[WireMessage]:
        """Run ``coro`` to completion while streaming its emitter messages.

        The driven coroutine publishes synchronously, so once it is done every
        message it produced is already queued — racing it against ``receive()``
        and then draining is therefore race-free.
        """
        task = asyncio.create_task(coro)
        self._active_task = task
        getter = asyncio.create_task(ui_side.receive())
        self._active_receiver = getter
        try:
            while True:
                done, _ = await asyncio.wait({task, getter}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    yield getter.result()
                    getter = asyncio.create_task(ui_side.receive())
                    self._active_receiver = getter
                    continue
                break
        finally:
            getter.cancel()
            with contextlib.suppress(BaseException):
                await getter
            if not task.done():
                task.cancel()
            # Observe the producer even when receiving failed first and it
            # already settled; otherwise its exception becomes an orphan.
            with contextlib.suppress(BaseException):
                await task
            self._active_task = None
            self._active_receiver = None
        while True:
            message = ui_side.try_receive_nowait()
            if message is None:
                break
            yield message
        await task

    async def _start_turn(
        self,
        text: str,
        content_params: list[dict[str, Any]] | None = None,
        *,
        new_session: bool = False,
    ) -> None:
        if new_session or self._engine_session_id is None:
            identity = (
                {"session_id": self._engine_session_id}
                if callable(getattr(self._manager, "get_event_emitter", None))
                else {}
            )
            self._engine_session_id = await self._manager.create_session(
                text or "",
                plan_mode=self._plan_mode,
                skills=self._skills,
                content_params=content_params,
                **identity,
            )
        else:
            await self._manager.reply_session(
                self._engine_session_id, text, content_params=content_params
            )
        self._record_turn(text)

    def _record_turn(self, text: str) -> None:
        """Append the user turn to the ACP session's context file.

        The ACP session identity lives in the ``Session`` store while the
        conversation is owned by ``SessionManager``. Recording each turn keeps
        ``session/list`` and history inspection meaningful for ACP sessions.
        """
        session = self._session
        if session is None or not text:
            return
        try:
            line = json.dumps({"role": "user", "content": [{"type": "text", "text": text}]})
            with open(session.context_file, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except Exception:
            logger.warning("Failed to record ACP turn in the session context file")

    def _classify(self) -> str:
        if self._engine_session_id is None:
            return _END_TURN
        entry = self._manager.get_session(self._engine_session_id)
        if entry is None:
            return _END_TURN
        status = str(getattr(entry, "status", "") or "").lower()
        if status == "ask_permission":
            return _ASK_PERMISSION
        if status in _QUESTION_STATUSES:
            return _ASK_USER
        return _END_TURN

    # -- pause bridging (mirrors WireServer) ---------------------------------
    def _pending_approvals(self) -> list[dict[str, Any]]:
        if self._engine_session_id is None:
            return []
        entry = self._manager.get_session(self._engine_session_id)
        items = list(getattr(entry, "ask_permissions", None) or [])
        return [item for item in items if isinstance(item, dict)]

    @staticmethod
    def _approval_request(item: dict[str, Any]) -> ApprovalRequest:
        return ApprovalRequest(
            id=f"apr_{uuid.uuid4().hex[:12]}",
            tool_call_id=str(item.get("toolCallId", "")),
            sender=str(item.get("name", "")),
            action=str(item.get("name", "")),
            description=str(item.get("description", "") or item.get("command", "")),
            display=list(item.get("display") or []),
            allow_session_approve=not bool(item.get("requiresExplicitApproval")),
        )

    async def _collect_permission_replies(
        self, requests: list[ApprovalRequest], items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        replies: list[dict[str, Any]] = []
        for request, item in zip(requests, items):
            try:
                response = await request.wait()
            except Exception:
                response = "reject"
            tool_call_id = item.get("toolCallId")
            if response in _APPROVE_RESPONSES:
                reply = {"toolCallId": tool_call_id, "permission": "allow"}
                if response == "approve_for_session":
                    reply["decision"] = response
                replies.append(reply)
            else:
                reply = {"toolCallId": tool_call_id, "permission": "deny"}
                if request.feedback:
                    reply["feedback"] = request.feedback
                replies.append(reply)
        return replies

    def _collect_questions(self) -> list[dict[str, Any]]:
        if self._engine_session_id is None:
            return []
        try:
            messages = self._manager.list_session_messages(self._engine_session_id)
        except Exception:
            return []
        latest_tool = next(
            (
                message
                for message in reversed(messages)
                if getattr(message, "role", "") == "tool"
                and not getattr(message, "compacted", False)
            ),
            None,
        )
        if latest_tool is None or not getattr(latest_tool, "content", ""):
            return []
        try:
            payload = json.loads(latest_tool.content)
            if isinstance(payload.get("metadata"), dict):
                questions = payload["metadata"].get("questions") or []
                if isinstance(questions, list):
                    return [q for q in questions if isinstance(q, dict)]
        except Exception:
            pass
        return []

    @staticmethod
    def _question_request(questions: list[dict[str, Any]]) -> QuestionRequest:
        return QuestionRequest(
            id=f"q_{uuid.uuid4().hex[:12]}",
            tool_call_id="",
            questions=[
                QuestionItem(
                    question=str(question.get("question", "")),
                    options=[
                        QuestionOption(
                            label=str(option.get("label", "")),
                            description=str(option.get("description", "")),
                        )
                        for option in (question.get("options") or [])
                        if isinstance(option, dict)
                    ],
                    header=str(question.get("header", "")),
                    multi_select=bool(question.get("multiSelect")),
                )
                for question in questions
            ],
        )

    @staticmethod
    def _format_answers(questions: list[dict[str, Any]], answers: dict[str, Any]) -> str:
        lines = ["<answers>"]
        for index, question in enumerate(questions):
            text = str(question.get("question", ""))
            answer = answers.get(str(index), "") or answers.get(text, "")
            lines.append(f"Q{index + 1} {text}: {answer}".rstrip())
        lines.append("</answers>")
        return "\n".join(lines)


__all__ = ["SessionManagerEngine", "build_acp_prompt"]
