"""Streaming handler that forwards LLM token deltas to the IPC server.

Emits a single phased ``turn`` event (``phase`` of ``start`` /
``reasoning`` / ``text`` / ``end``) so the Textual UI can render streaming
output incrementally. Set as ``agent.streaming_handler`` in
``coderAI/tui/session_setup.py``.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

# Match EventReducer.STREAM_FLUSH_S so IPC and UI batch at the same cadence.
STREAM_EMIT_S = 0.120


def _partial_tag_suffix_len(buffer: str, tag: str) -> int:
    """Return the length of the longest suffix of ``buffer`` that is a strict
    (non-empty, non-full) prefix of ``tag``.

    Used to decide how many trailing characters to hold back while we wait to
    see whether a ``<think>`` / ``</think>`` tag is forming. Capped at
    ``len(tag) - 1`` so a literal ``<`` followed by content that obviously
    doesn't lead into ``tag`` is flushed immediately rather than buffered.
    """
    max_prefix = min(len(buffer), len(tag) - 1)
    for n in range(max_prefix, 0, -1):
        if tag.startswith(buffer[-n:]):
            return n
    return 0


class BridgeStreamingHandler:
    """Consume an LLM stream and relay deltas through an ``UIBridge``."""

    def __init__(self, server) -> None:
        self.server = server
        self.current_content = ""
        self.current_reasoning = ""
        self.tool_calls: List[Dict[str, Any]] = []
        self._in_reasoning = False
        self._reasoning_type: Optional[str] = None
        self._tag_buffer = ""
        self._raw_content = ""
        self._raw_reasoning = ""
        self._batch_text = ""
        self._batch_reasoning = ""
        self._batch_last_flush = 0.0

    async def handle_stream(
        self, stream, initial_content: str = "", cancel_event: Any = None
    ) -> Dict[str, Any]:
        self.current_content = initial_content
        self.current_reasoning = ""
        self.tool_calls = []
        self._in_reasoning = False
        self._reasoning_type = None
        self._tag_buffer = ""
        self._raw_content = initial_content
        self._raw_reasoning = ""
        self._batch_text = ""
        self._batch_reasoning = ""
        self._batch_last_flush = time.monotonic()

        start_time = time.monotonic()
        finish_reason: Optional[str] = None
        self.server.emit("turn", phase="start", reasoningActive=False)

        if initial_content:
            self._queue_emit(initial_content, reasoning=False)

        try:
            async for chunk in stream:
                if cancel_event is not None and cancel_event.is_set():
                    finish_reason = "cancelled"
                    break
                choices = chunk.get("choices", [])
                if not choices:
                    continue
                choice = choices[0]
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                delta = choice.get("delta", {})

                reasoning_delta = self._coalesce_chunk(
                    delta.get("reasoning_content", "") or "",
                    attr="_raw_reasoning",
                )
                if reasoning_delta:
                    if not self._in_reasoning:
                        self._in_reasoning = True
                        self._reasoning_type = "field"
                    self.current_reasoning += reasoning_delta
                    self._queue_emit(reasoning_delta, reasoning=True)

                content_chunk = self._coalesce_chunk(
                    delta.get("content", "") or "",
                    attr="_raw_content",
                )

                if content_chunk:
                    if self._in_reasoning and self._reasoning_type == "field":
                        self._in_reasoning = False
                        self._reasoning_type = None

                    self._tag_buffer += content_chunk

                    while True:
                        if not self._in_reasoning:
                            if "<think>" in self._tag_buffer:
                                before, after = self._tag_buffer.split("<think>", 1)
                                if before:
                                    self.current_content += before
                                    self._queue_emit(before, reasoning=False)
                                self._in_reasoning = True
                                self._reasoning_type = "tag"
                                self._tag_buffer = after
                            else:
                                break
                        else:
                            if "</think>" in self._tag_buffer:
                                before, after = self._tag_buffer.split("</think>", 1)
                                self.current_reasoning += before
                                self._queue_emit(before, reasoning=True)
                                self._in_reasoning = False
                                self._reasoning_type = None
                                self._tag_buffer = after
                            else:
                                break

                    if not self._in_reasoning:
                        hold = _partial_tag_suffix_len(self._tag_buffer, "<think>")
                        if hold:
                            flush = self._tag_buffer[:-hold]
                            self._tag_buffer = self._tag_buffer[-hold:]
                        else:
                            flush = self._tag_buffer
                            self._tag_buffer = ""
                        if flush:
                            self.current_content += flush
                            self._queue_emit(flush, reasoning=False)
                    else:
                        hold = _partial_tag_suffix_len(self._tag_buffer, "</think>")
                        if hold:
                            flush = self._tag_buffer[:-hold]
                            self._tag_buffer = self._tag_buffer[-hold:]
                        else:
                            flush = self._tag_buffer
                            self._tag_buffer = ""
                        if flush:
                            self.current_reasoning += flush
                            self._queue_emit(flush, reasoning=True)

                if delta.get("tool_calls"):
                    for tcd in delta["tool_calls"]:
                        idx = tcd.get("index", 0)
                        while len(self.tool_calls) <= idx:
                            self.tool_calls.append(
                                {
                                    "id": f"call_{uuid.uuid4().hex[:24]}",
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                }
                            )
                        if tcd.get("id"):
                            self.tool_calls[idx]["id"] = tcd["id"]
                        if "function" in tcd:
                            fn = tcd["function"]
                            if fn.get("name"):
                                self.tool_calls[idx]["function"]["name"] += fn["name"]
                            if fn.get("arguments"):
                                self.tool_calls[idx]["function"]["arguments"] += fn["arguments"]
        finally:
            self._flush_tag_buffer()
            self._flush_emit_batch(force=True)

            elapsed_ms = int((time.monotonic() - start_time) * 1000)
            self.server.emit(
                "turn",
                phase="end",
                elapsedMs=elapsed_ms,
                reasoningActive=bool(self.current_reasoning),
            )

        return {
            "content": self.current_content,
            "tool_calls": self.tool_calls if self.tool_calls else None,
            "finish_reason": finish_reason,
            "reasoning_content": self.current_reasoning if self.current_reasoning else None,
        }

    def _flush_tag_buffer(self) -> None:
        leftover = self._tag_buffer
        self._tag_buffer = ""
        if not leftover:
            return
        if self._in_reasoning:
            self.current_reasoning += leftover
            self._queue_emit(leftover, reasoning=True)
        else:
            self.current_content += leftover
            self._queue_emit(leftover, reasoning=False)

    def _queue_emit(self, text: str, *, reasoning: bool) -> None:
        if not text:
            return
        if reasoning:
            self._batch_reasoning += text
        else:
            self._batch_text += text
        self._flush_emit_batch()

    def _flush_emit_batch(self, *, force: bool = False) -> None:
        now = time.monotonic()
        pending = len(self._batch_text) + len(self._batch_reasoning)
        if not force and pending == 0:
            return
        if not force and (now - self._batch_last_flush) < STREAM_EMIT_S and pending < 512:
            return
        if self._batch_text:
            self.server.emit(
                "turn",
                phase="text",
                delta=self._batch_text,
                reasoningActive=bool(self.current_reasoning),
            )
            self._batch_text = ""
        if self._batch_reasoning:
            self.server.emit(
                "turn",
                phase="reasoning",
                delta=self._batch_reasoning,
                reasoningActive=True,
            )
            self._batch_reasoning = ""
        self._batch_last_flush = now

    def _coalesce_chunk(self, chunk: str, *, attr: str) -> str:
        if not chunk:
            return ""

        previous = getattr(self, attr)
        if previous and chunk == previous:
            return ""
        if previous and len(chunk) > len(previous) and chunk.startswith(previous):
            setattr(self, attr, chunk)
            return chunk[len(previous) :]

        setattr(self, attr, previous + chunk)
        return chunk
