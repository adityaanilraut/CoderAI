"""Headless client for driving CoderAI sessions without a terminal.

The turn protocol mirrors :class:`coderai.acp.engine.SessionManagerEngine`
(which itself mirrors ``coderai.wire.server.WireServer``): :meth:`prompt`
streams an async iterator of wire messages and resolves permission/question
pauses inline using the configured policy, so callers never need a terminal.

Only the standard library is imported at module load; the live engine
(``coderai``) and ``kosong`` content parts are imported lazily so the package
stays importable on its own (unit tests inject a fake engine).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any, Union

from coderai_sdk.models import ChatMessage, TurnResult

#: Reject every permission request (default: fail closed, no terminal to ask).
DENY_ALL = "deny"
#: Approve every permission request (equivalent to YOLO for this client).
ALLOW_ALL = "allow"

#: ``"deny"`` | ``"allow"`` | a callable receiving the request, returning
#: ``"approve"``, ``"approve_for_session"``, or ``"reject"``.
PermissionPolicy = Union[str, Callable[[Any], str]]
#: Callable receiving a question request, returning ``{index_or_text: answer}``.
QuestionHandler = Callable[[Any], dict]
#: Sync or async callable receiving every streamed wire message.
EventCallback = Callable[[Any], Any]

_APPROVAL_NAMES = {"ApprovalRequest"}
_QUESTION_NAMES = {"QuestionRequest"}
_THINK_NAMES = {"ThinkPart"}
_TEXT_NAMES = {"TextPart"}
_TOOL_CALL_NAMES = {"ToolCall", "ToolCallPart"}
_TOOL_RESULT_NAMES = {"ToolResult", "ToolResultPart"}
_VALID_DECISIONS = ("approve", "approve_for_session", "reject")


class CoderAIClient:
    """Drive one CoderAI session headlessly.

    :param engine: live engine exposing ``run(parts, cancel_event)`` plus the
        ``SessionManagerEngine`` accessors (``session_id``, ``bind_session``,
        ``interrupt``, ``set_model``). Injected directly in tests; built via
        :meth:`create` for real usage.
    :param permission_policy: how to answer ``ApprovalRequest`` pauses.
    :param question_handler: how to answer ``QuestionRequest`` pauses
        (default: empty answers).
    :param on_event: optional sync/async observer for every wire message.
    """

    def __init__(
        self,
        *,
        engine: Any = None,
        permission_policy: PermissionPolicy = DENY_ALL,
        question_handler: QuestionHandler | None = None,
        on_event: EventCallback | None = None,
    ) -> None:
        self._engine = engine
        self._permission_policy = permission_policy
        self._question_handler = question_handler
        self._on_event = on_event

    @classmethod
    def create(
        cls,
        work_dir: str = ".",
        *,
        model: str | None = None,
        plan_mode: bool = False,
        skills: list[str] | None = None,
        permission_policy: PermissionPolicy = DENY_ALL,
        question_handler: QuestionHandler | None = None,
        on_event: EventCallback | None = None,
    ) -> CoderAIClient:
        """Build a client on the live ``SessionManager`` engine (Stack A)."""
        from coderai.acp.engine import SessionManagerEngine
        from coderai.cli.session_factory import build_session_manager

        manager = build_session_manager(str(work_dir), model=model, non_interactive=True)
        engine = SessionManagerEngine(manager, plan_mode=plan_mode, skills=skills)
        return cls(
            engine=engine,
            permission_policy=permission_policy,
            question_handler=question_handler,
            on_event=on_event,
        )

    # -- accessors --------------------------------------------------------
    @property
    def engine(self) -> Any:
        return self._engine

    @property
    def session_id(self) -> str | None:
        return getattr(self._engine, "session_id", None)

    def bind_session(self, session_id: str) -> None:
        """Attach to an existing engine session (resume/fork)."""
        engine = self._require_engine()
        bind = getattr(engine, "bind_session", None)
        if bind is None:
            raise RuntimeError("Bound engine does not support bind_session()")
        bind(session_id)

    def interrupt(self) -> None:
        """Ask the engine to stop the in-flight turn (no-op without engine)."""
        if self._engine is None:
            return
        with contextlib.suppress(Exception):
            self._engine.interrupt()

    def set_model(self, model: str) -> None:
        self._require_engine().set_model(model)

    # -- turns ------------------------------------------------------------
    async def prompt(self, text: str | list[Any], *, timeout: float | None = None) -> TurnResult:
        """Run one headless turn and collect its outcome."""
        engine = self._require_engine()
        run = getattr(engine, "run", None)
        if run is None:
            raise RuntimeError("Bound engine does not implement run()")
        parts = self._to_parts(text)
        cancel = asyncio.Event()
        result = TurnResult()

        async def _drive() -> None:
            async for message in run(parts, cancel):
                self._handle_message(message, result)
                await self._emit(message)

        if timeout is None:
            await _drive()
        else:
            await asyncio.wait_for(_drive(), timeout)
        result.session_id = getattr(engine, "session_id", None)
        return result

    # -- internals --------------------------------------------------------
    def _require_engine(self) -> Any:
        if self._engine is None:
            raise RuntimeError("No engine bound; use CoderAIClient.create() or engine=...")
        return self._engine

    @staticmethod
    def _to_parts(text: str | list[Any]) -> list[Any]:
        if not isinstance(text, str):
            return list(text)
        try:
            from kosong.message import TextPart
        except ImportError:
            return [{"type": "text", "text": text}]
        return [TextPart(text=text)]

    def _handle_message(self, message: Any, result: TurnResult) -> None:
        result.events.append(message)
        if self._is_approval(message):
            self._resolve_permission(message)
            return
        if self._is_question(message):
            self._resolve_questions(message)
            return
        name = type(message).__name__
        if name in _THINK_NAMES:
            text = getattr(message, "think", None) or getattr(message, "text", "")
            if text:
                result.thinking += str(text)
            return
        if name in _TEXT_NAMES or isinstance(getattr(message, "text", None), str):
            text = str(getattr(message, "text", "") or "")
            if text:
                result.messages.append(ChatMessage(role="assistant", content=text))
                result.text += text
            return
        if name in _TOOL_CALL_NAMES:
            result.tool_calls.append(self._tool_call_info(message))
            return
        if name in _TOOL_RESULT_NAMES:
            output = getattr(message, "output", "") or getattr(message, "content", "")
            result.messages.append(ChatMessage(role="tool", content=str(output)))
            return

    @staticmethod
    def _is_approval(message: Any) -> bool:
        try:
            from coderai.wire.types import ApprovalRequest
        except ImportError:
            ApprovalRequest = None  # type: ignore[assignment]
        if ApprovalRequest is not None and isinstance(message, ApprovalRequest):
            return True
        return type(message).__name__ in _APPROVAL_NAMES

    @staticmethod
    def _is_question(message: Any) -> bool:
        try:
            from coderai.wire.types import QuestionRequest
        except ImportError:
            QuestionRequest = None  # type: ignore[assignment]
        if QuestionRequest is not None and isinstance(message, QuestionRequest):
            return True
        return type(message).__name__ in _QUESTION_NAMES

    @staticmethod
    def _tool_call_info(message: Any) -> dict[str, Any]:
        call_id = getattr(message, "id", "") or getattr(message, "tool_call_id", "")
        name = getattr(message, "name", "") or getattr(message, "function", "")
        return {"id": str(call_id), "name": str(name)}

    def _resolve_permission(self, request: Any) -> None:
        policy = self._permission_policy
        try:
            if callable(policy):
                decision = policy(request)
            elif policy == ALLOW_ALL:
                decision = "approve"
            else:
                decision = "reject"
        except Exception:
            decision = "reject"
        if decision not in _VALID_DECISIONS:
            decision = "reject"
        request.resolve(decision)

    def _resolve_questions(self, request: Any) -> None:
        try:
            answers = self._question_handler(request) if self._question_handler else {}
        except Exception:
            answers = {}
        if not isinstance(answers, dict):
            answers = {}
        request.resolve(answers)

    async def _emit(self, message: Any) -> None:
        if self._on_event is None:
            return
        outcome = self._on_event(message)
        if asyncio.iscoroutine(outcome):
            await outcome


__all__ = ["ALLOW_ALL", "CoderAIClient", "DENY_ALL"]
