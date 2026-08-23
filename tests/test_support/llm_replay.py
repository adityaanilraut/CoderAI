"""Deterministic LLM replay engine for zero-cost, keyless automated testing.

Port of dsh test-support/llm-replay. Enables recording and replaying LLM responses
(text streams, reasoning/thinking, tool calls, token usage) in unit and integration tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from collections.abc import Callable, Iterator


@dataclass
class ReplayChoiceMessage:
    content: str = ""
    role: str = "assistant"
    tool_calls: list[dict[str, Any]] | None = None
    reasoning_content: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        if self.reasoning_content:
            d["reasoning_content"] = self.reasoning_content
        return d


@dataclass
class ReplayChoice:
    message: ReplayChoiceMessage
    finish_reason: str = "stop"
    index: int = 0


@dataclass
class ReplayChunkDelta:
    content: str | None = None
    role: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    reasoning_content: str | None = None


@dataclass
class ReplayChunkChoice:
    delta: ReplayChunkDelta
    finish_reason: str | None = None
    index: int = 0


@dataclass
class ReplayChunk:
    choices: list[ReplayChunkChoice]
    id: str = "chatcmpl-replay-chunk"
    created: int = 1700000000
    model: str = "replay-model"


@dataclass
class ReplayResponse:
    id: str = "chatcmpl-replay"
    choices: list[ReplayChoice] = field(default_factory=list)
    created: int = 1700000000
    model: str = "replay-model"
    usage: dict[str, int] = field(
        default_factory=lambda: {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        }
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "choices": [
                {
                    "index": c.index,
                    "finish_reason": c.finish_reason,
                    "message": c.message.to_dict(),
                }
                for c in self.choices
            ],
            "created": self.created,
            "model": self.model,
            "usage": self.usage,
        }


class ReplayChatCompletions:
    """Mock OpenAI chat.completions namespace supporting both sync and streaming."""

    def __init__(self, entries: list[dict[str, Any] | str | ReplayResponse]) -> None:
        self.entries = list(entries)
        self.call_history: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.call_history.append(kwargs)
        if not self.entries:
            resp = ReplayResponse(
                choices=[
                    ReplayChoice(message=ReplayChoiceMessage(content="Replay default response."))
                ]
            )
            return resp

        next_entry = self.entries.pop(0)

        if isinstance(next_entry, Exception):
            raise next_entry

        if isinstance(next_entry, str):
            resp = ReplayResponse(
                choices=[ReplayChoice(message=ReplayChoiceMessage(content=next_entry))]
            )
            if kwargs.get("stream"):
                return self._stream_chunks(next_entry)
            return resp

        if isinstance(next_entry, ReplayResponse):
            if kwargs.get("stream"):
                content = next_entry.choices[0].message.content if next_entry.choices else ""
                return self._stream_chunks(content)
            return next_entry

        if isinstance(next_entry, dict):
            content = next_entry.get("content", "")
            tool_calls = next_entry.get("tool_calls")
            reasoning = next_entry.get("reasoning_content") or next_entry.get("thinking")
            finish_reason = next_entry.get("finish_reason", "tool_calls" if tool_calls else "stop")
            usage = next_entry.get("usage") or {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
            }

            resp = ReplayResponse(
                id=next_entry.get("id", "chatcmpl-replay"),
                choices=[
                    ReplayChoice(
                        message=ReplayChoiceMessage(
                            content=content,
                            tool_calls=tool_calls,
                            reasoning_content=reasoning,
                        ),
                        finish_reason=finish_reason,
                    )
                ],
                model=next_entry.get("model", "replay-model"),
                usage=usage,
            )

            if kwargs.get("stream"):
                return self._stream_dict_chunks(next_entry)
            return resp

        return ReplayResponse(
            choices=[ReplayChoice(message=ReplayChoiceMessage(content=str(next_entry)))]
        )

    def _stream_chunks(self, text: str) -> Iterator[ReplayChunk]:
        words = text.split(" ")
        for i, word in enumerate(words):
            chunk_text = word + (" " if i < len(words) - 1 else "")
            yield ReplayChunk(
                choices=[
                    ReplayChunkChoice(
                        delta=ReplayChunkDelta(content=chunk_text),
                        finish_reason=None if i < len(words) - 1 else "stop",
                    )
                ]
            )

    def _stream_dict_chunks(self, entry: dict[str, Any]) -> Iterator[ReplayChunk]:
        reasoning = entry.get("reasoning_content") or entry.get("thinking")
        if reasoning:
            yield ReplayChunk(
                choices=[
                    ReplayChunkChoice(
                        delta=ReplayChunkDelta(reasoning_content=reasoning),
                        finish_reason=None,
                    )
                ]
            )

        content = entry.get("content", "")
        if content:
            yield ReplayChunk(
                choices=[
                    ReplayChunkChoice(
                        delta=ReplayChunkDelta(content=content),
                        finish_reason=None,
                    )
                ]
            )

        tool_calls = entry.get("tool_calls")
        if tool_calls:
            yield ReplayChunk(
                choices=[
                    ReplayChunkChoice(
                        delta=ReplayChunkDelta(tool_calls=tool_calls),
                        finish_reason="tool_calls",
                    )
                ]
            )
        else:
            yield ReplayChunk(
                choices=[
                    ReplayChunkChoice(
                        delta=ReplayChunkDelta(),
                        finish_reason="stop",
                    )
                ]
            )


class ReplayChat:
    def __init__(self, entries: list[Any]) -> None:
        self.completions = ReplayChatCompletions(entries)


class ReplayClient:
    """Mock client satisfying OpenAI SDK interface."""

    def __init__(self, entries: list[Any]) -> None:
        self.chat = ReplayChat(entries)

    def set_entries(self, entries: list[Any]) -> None:
        self.chat.completions.entries = list(entries)

    @property
    def call_history(self) -> list[dict[str, Any]]:
        return self.chat.completions.call_history


def create_replay_client_factory(
    entries: list[Any], model: str = "replay-gpt-4o"
) -> Callable[[], dict[str, Any]]:
    """Return a create_openai_client factory configured with a deterministic ReplayClient."""
    client = ReplayClient(entries)

    def factory() -> dict[str, Any]:
        return {
            "client": client,
            "model": model,
            "baseURL": "https://replay.local/v1",
            "temperature": 0.0,
            "thinkingEnabled": False,
            "reasoningEffort": "low",
            "debugLogEnabled": False,
            "telemetryEnabled": False,
            "notify": None,
            "webSearchTool": None,
            "env": {},
        }

    return factory
