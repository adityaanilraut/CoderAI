"""Self-contained tests for the headless CoderAI SDK (no engine, no network)."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "src"),
)

from coderai_sdk import ALLOW_ALL, DENY_ALL, ChatMessage, CoderAIClient, SessionInfo, TurnResult


# -- fakes -----------------------------------------------------------------


@dataclass
class TextPart:
    text: str = ""


@dataclass
class ThinkPart:
    text: str = ""


@dataclass
class ApprovalRequest:
    action: str = "shell"
    description: str = "run tests"
    decision: str | None = None

    def resolve(self, decision: str, feedback: str = "") -> None:
        self.decision = decision


@dataclass
class QuestionRequest:
    questions: list[str] = field(default_factory=list)
    answers: dict[str, str] | None = None

    def resolve(self, answers: dict[str, str]) -> None:
        self.answers = answers


@dataclass
class ToolCallPart:
    id: str = "call-1"
    name: str = "read"


class FakeEngine:
    """Duck-typed stand-in for ``SessionManagerEngine``."""

    def __init__(self, script: list[Any]) -> None:
        self.script = script
        self.session_id = "sess-123"
        self.seen_parts: list[Any] | None = None
        self.interrupted = False

    async def run(self, parts: list[Any], cancel_event: Any) -> Any:
        self.seen_parts = list(parts)
        for message in self.script:
            yield message

    def bind_session(self, session_id: str) -> None:
        self.session_id = session_id

    def interrupt(self) -> None:
        self.interrupted = True

    def set_model(self, model: str) -> None:
        self.model = model


# -- tests -----------------------------------------------------------------


async def test_prompt_collects_assistant_text() -> None:
    engine = FakeEngine([TextPart(text="Hello, "), TextPart(text="world")])
    client = CoderAIClient(engine=engine)
    result = await client.prompt("hi")
    assert isinstance(result, TurnResult)
    assert result.text == "Hello, world"
    assert result.session_id == "sess-123"
    assert [m.content for m in result.messages] == ["Hello, ", "world"]
    assert all(m.role == "assistant" for m in result.messages)
    assert len(result.events) == 2
    assert engine.seen_parts is not None and len(engine.seen_parts) == 1


async def test_prompt_collects_thinking() -> None:
    engine = FakeEngine([ThinkPart(text="hmm"), TextPart(text="done")])
    result = await CoderAIClient(engine=engine).prompt("think")
    assert result.thinking == "hmm"
    assert result.text == "done"


async def test_permission_default_is_deny() -> None:
    request = ApprovalRequest()
    result = await CoderAIClient(engine=FakeEngine([request])).prompt("go")
    assert request.decision == "reject"
    assert result.events == [request]


async def test_permission_allow_all() -> None:
    request = ApprovalRequest()
    client = CoderAIClient(engine=FakeEngine([request]), permission_policy=ALLOW_ALL)
    await client.prompt("go")
    assert request.decision == "approve"


async def test_permission_callable_and_invalid_falls_back_to_deny() -> None:
    seen: list[Any] = []

    def policy(request: Any) -> str:
        seen.append(request)
        return "approve_for_session"

    first = ApprovalRequest()
    await CoderAIClient(engine=FakeEngine([first]), permission_policy=policy).prompt("go")
    assert first.decision == "approve_for_session"
    assert seen == [first]

    second = ApprovalRequest()
    await CoderAIClient(engine=FakeEngine([second]), permission_policy="bogus").prompt("go")
    assert second.decision == "reject"


async def test_question_handler_defaults_to_empty_answers() -> None:
    request = QuestionRequest(questions=["Pick one?"])
    await CoderAIClient(engine=FakeEngine([request])).prompt("ask")
    assert request.answers == {}


async def test_question_handler_custom_answers() -> None:
    request = QuestionRequest(questions=["Pick one?"])
    client = CoderAIClient(
        engine=FakeEngine([request]),
        question_handler=lambda req: {"0": "first"},
    )
    await client.prompt("ask")
    assert request.answers == {"0": "first"}


async def test_tool_calls_recorded() -> None:
    engine = FakeEngine([ToolCallPart(id="c1", name="read"), TextPart(text="ok")])
    result = await CoderAIClient(engine=engine).prompt("read it")
    assert result.tool_calls == [{"id": "c1", "name": "read"}]
    assert result.text == "ok"


async def test_on_event_receives_every_message() -> None:
    script = [TextPart(text="a"), ApprovalRequest(), TextPart(text="b")]
    seen_sync: list[Any] = []
    seen_async: list[Any] = []

    async def on_event_async(message: Any) -> None:
        seen_async.append(message)

    client = CoderAIClient(engine=FakeEngine(script), on_event=seen_sync.append)
    await client.prompt("go")
    assert seen_sync == script

    client_async = CoderAIClient(engine=FakeEngine(script), on_event=on_event_async)
    await client_async.prompt("go")
    assert seen_async == script


async def test_bind_interrupt_and_set_model_delegate_to_engine() -> None:
    engine = FakeEngine([])
    client = CoderAIClient(engine=engine)
    client.bind_session("sess-999")
    assert client.session_id == "sess-999"
    client.interrupt()
    assert engine.interrupted is True
    client.set_model("some-model")
    assert engine.model == "some-model"


async def test_prompt_without_engine_raises() -> None:
    with pytest.raises(RuntimeError):
        await CoderAIClient().prompt("hi")


def test_models_defaults() -> None:
    message = ChatMessage(role="assistant")
    assert message.content == ""
    assert TurnResult().session_id is None
    assert SessionInfo(session_id="s").status == ""
    assert DENY_ALL == "deny"
    assert ALLOW_ALL == "allow"
