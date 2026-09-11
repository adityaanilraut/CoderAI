"""Tests for the Stack A (``SessionManager``) ACP engine adapter.

Covers :mod:`coderai.acp.engine`: prompt flattening, emitter streaming,
permission/question pause bridging, history-replay suppression, and the
accessors ``acp/server.py`` + ``acp/session.py`` depend on.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from kosong.message import TextPart as KTextPart

from coderai.acp.engine import SessionManagerEngine, build_prompt
from coderai.wire.emitter import get_emitter
from coderai.wire.types import ApprovalRequest, QuestionRequest, TextPart

TIMEOUT = 10.0


class _FakeManager:
    """Minimal stand-in for ``SessionManager`` (only the ACP-used surface)."""

    def __init__(self, *, pause: str | None = None) -> None:
        self.sessions: dict[str, SimpleNamespace] = {}
        self.responded: list[list[dict]] = []
        self.interrupted: list[str] = []
        self.prompts: list[tuple[str, str | None]] = []
        self.models: list[str] = []
        self.messages: list[object] = []
        self.ask_permissions: list[dict] = []
        self._pause = pause
        self._pause_served = False

    def _entry(self) -> SimpleNamespace:
        """Pause once (as a real turn does), then report a terminal status."""
        if not self._pause_served:
            if self._pause == "permission":
                self._pause_served = True
                return SimpleNamespace(status="ask_permission", ask_permissions=self.ask_permissions)
            if self._pause == "question":
                self._pause_served = True
                return SimpleNamespace(status="ask_user_question", ask_permissions=[])
        return SimpleNamespace(status="completed", ask_permissions=[])

    async def create_session(self, user_prompt, plan_mode=False, skills=None):
        self.prompts.append(("create", user_prompt))
        get_emitter().turn_begin(user_prompt)
        get_emitter().text(f"echo: {user_prompt}")
        self.sessions["s1"] = self._entry()
        return "s1"

    async def reply_session(self, session_id, user_prompt=None, **kwargs):
        self.prompts.append(("reply", user_prompt))
        if user_prompt:
            get_emitter().text(f"reply: {user_prompt}")
        self.sessions[session_id] = self._entry()

    def get_session(self, session_id):
        return self.sessions.get(session_id)

    async def respond_permissions(self, session_id, replies):
        self.responded.append(replies)
        self.sessions[session_id] = SimpleNamespace(status="completed", ask_permissions=[])

    def list_session_messages(self, session_id):
        return self.messages

    def get_resolved_settings(self):
        return {"models": {}, "providers": {}}

    def get_active_model(self):
        return "test-model"

    def set_model(self, model_name):
        self.models.append(model_name)

    def interrupt_session(self, session_id):
        self.interrupted.append(session_id)


async def _collect(engine: SessionManagerEngine, text: str = "hi", cancel: asyncio.Event | None = None):
    async def _drain():
        return [msg async for msg in engine.run([KTextPart(text=text)], cancel or asyncio.Event())]

    return await asyncio.wait_for(_drain(), timeout=TIMEOUT)


def test_build_prompt_flattens_text_parts():
    assert build_prompt([KTextPart(text="first"), KTextPart(text="second")]) == "first\nsecond"


def test_build_prompt_ignores_empty_text():
    assert build_prompt([KTextPart(text="")]) == ""


@pytest.mark.asyncio
async def test_engine_streams_turn_messages():
    manager = _FakeManager()
    engine = SessionManagerEngine(manager)

    messages = await _collect(engine, "hello")

    assert "TextPart" in [type(message).__name__ for message in messages]
    texts = [message.text for message in messages if isinstance(message, TextPart)]
    assert any("echo: hello" in text for text in texts)
    assert engine.session_id == "s1"
    assert manager.prompts == [("create", "hello")]


@pytest.mark.asyncio
async def test_engine_uses_reply_on_second_turn():
    manager = _FakeManager()
    engine = SessionManagerEngine(manager)

    await _collect(engine, "first")
    await _collect(engine, "second")

    assert manager.prompts == [("create", "first"), ("reply", "second")]


@pytest.mark.asyncio
async def test_engine_does_not_replay_earlier_turn_messages():
    """BroadcastQueue replays history on subscribe; the engine must suppress it."""
    manager = _FakeManager()
    engine = SessionManagerEngine(manager)

    await _collect(engine, "first")
    second = await _collect(engine, "second")

    texts = [m.text for m in second if isinstance(m, TextPart)]
    assert all("first" not in text for text in texts), texts
    assert any("second" in text for text in texts)


@pytest.mark.asyncio
async def test_engine_bridges_permission_approval():
    manager = _FakeManager(pause="permission")
    manager.ask_permissions = [{"toolCallId": "tc1", "name": "bash", "description": "run ls"}]
    engine = SessionManagerEngine(manager)

    seen_requests: list[ApprovalRequest] = []

    async def _consume():
        async for message in engine.run([KTextPart(text="go")], asyncio.Event()):
            if isinstance(message, ApprovalRequest):
                seen_requests.append(message)
                message.resolve("approve")

    await asyncio.wait_for(_consume(), timeout=TIMEOUT)

    assert len(seen_requests) == 1
    assert seen_requests[0].tool_call_id == "tc1"
    assert seen_requests[0].action == "bash"
    assert manager.responded == [[{"toolCallId": "tc1", "permission": "allow"}]]


@pytest.mark.asyncio
async def test_engine_bridges_permission_denial_with_feedback():
    manager = _FakeManager(pause="permission")
    manager.ask_permissions = [{"toolCallId": "tc9", "name": "write"}]
    engine = SessionManagerEngine(manager)

    async def _consume():
        async for message in engine.run([KTextPart(text="go")], asyncio.Event()):
            if isinstance(message, ApprovalRequest):
                message.resolve("reject", "not allowed")

    await asyncio.wait_for(_consume(), timeout=TIMEOUT)

    assert manager.responded == [
        [{"toolCallId": "tc9", "permission": "deny", "feedback": "not allowed"}]
    ]


@pytest.mark.asyncio
async def test_engine_bridges_question_pause():
    manager = _FakeManager(pause="question")
    manager.messages = [
        SimpleNamespace(
            role="tool",
            compacted=False,
            content='{"metadata": {"questions": [{"question": "Which?", "options": []}]}}',
        )
    ]
    engine = SessionManagerEngine(manager)

    seen: list[QuestionRequest] = []

    async def _consume():
        async for message in engine.run([KTextPart(text="go")], asyncio.Event()):
            if isinstance(message, QuestionRequest):
                seen.append(message)
                message.resolve({"0": "option A"})

    await asyncio.wait_for(_consume(), timeout=TIMEOUT)

    assert len(seen) == 1
    assert seen[0].questions[0].question == "Which?"
    assert ("reply", "<answers>\nQ1 Which?: option A\n</answers>") in manager.prompts


@pytest.mark.asyncio
async def test_engine_interrupts_in_flight_turn_on_cancel():
    """A cancelled prompt must interrupt the bound SessionManager session."""
    manager = _FakeManager()
    stop = asyncio.Event()

    async def _slow_reply(session_id, user_prompt=None, **kwargs):
        manager.prompts.append(("reply", user_prompt))
        await stop.wait()
        manager.sessions[session_id] = SimpleNamespace(status="interrupted", ask_permissions=[])

    manager.reply_session = _slow_reply  # type: ignore[method-assign]

    def _interrupt(session_id):
        manager.interrupted.append(session_id)
        stop.set()

    manager.interrupt_session = _interrupt  # type: ignore[method-assign]

    engine = SessionManagerEngine(manager)
    await _collect(engine, "first")  # binds engine.session_id == "s1"

    cancel = asyncio.Event()

    async def _cancel_soon():
        await asyncio.sleep(0.05)
        cancel.set()

    canceller = asyncio.create_task(_cancel_soon())
    await _collect(engine, "second", cancel)
    await canceller

    assert manager.interrupted == ["s1"]


@pytest.mark.asyncio
async def test_engine_accessors_and_binding():
    manager = _FakeManager()
    engine = SessionManagerEngine(manager, config="CFG", session="SESS")

    assert engine.config == "CFG"
    assert engine.session == "SESS"
    assert engine.toolset is None
    assert engine.llm is None
    assert engine.session_id is None

    engine.bind_session("ses_abc")
    assert engine.session_id == "ses_abc"

    engine.set_model("gpt-x")
    assert manager.models == ["gpt-x"]

    assert engine.is_oauth_session() is False  # must not raise on a bare manager
