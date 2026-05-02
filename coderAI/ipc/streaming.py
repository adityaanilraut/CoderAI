"""Streaming handler that forwards LLM token deltas to the IPC server.

Emits a single phased ``turn`` NDJSON event (``phase`` of ``start`` /
``reasoning`` / ``text`` / ``end``) so the Ink UI can render streaming
output in React. Used when ``agent.streaming_handler`` is set from
``coderAI.ipc.entry`` (interactive mode).
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional


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


class IPCStreamingHandler:
    """Consume an LLM stream and relay deltas through an ``IPCServer``."""

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

    async def handle_stream(
        self, stream, initial_content: str = ""
    ) -> Dict[str, Any]:
        self.current_content = initial_content
        self.current_reasoning = ""
        self.tool_calls = []
        self._in_reasoning = False
        self._reasoning_type = None
        self._tag_buffer = ""
        self._raw_content = initial_content
        self._raw_reasoning = ""

        start_time = time.monotonic()
        finish_reason: Optional[str] = None
        self.server.emit("turn", phase="start", reasoningActive=False)

        if initial_content:
            self._emit(initial_content, reasoning=False)

        try:
            async for chunk in stream:
                choices = chunk.get("choices", [])
                if not choices:
                    continue
                choice = choices[0]
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                delta = choice.get("delta", {})

                # Reasoning field (Anthropic / OpenAI o-series extensions)
                reasoning_delta = self._coalesce_chunk(
                    delta.get("reasoning_content", "") or "",
                    attr="_raw_reasoning",
                )
                if reasoning_delta:
                    if not self._in_reasoning:
                        self._in_reasoning = True
                        self._reasoning_type = "field"
                    self.current_reasoning += reasoning_delta
                    self._emit(reasoning_delta, reasoning=True)

                # Regular content (may contain <think> tags inline)
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
                                    self._emit(before, reasoning=False)
                                self._in_reasoning = True
                                self._reasoning_type = "tag"
                                self._tag_buffer = after
                            else:
                                break
                        else:
                            if "</think>" in self._tag_buffer:
                                before, after = self._tag_buffer.split("</think>", 1)
                                self.current_reasoning += before
                                self._emit(before, reasoning=True)
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
                            self._emit(flush, reasoning=False)
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
                            self._emit(flush, reasoning=True)

                # Accumulate tool calls (emitted via event_emitter when executed)
                if delta.get("tool_calls"):
                    for tcd in delta["tool_calls"]:
                        idx = tcd.get("index", 0)
                        while len(self.tool_calls) <= idx:
                            self.tool_calls.append({
                                "id": f"call_{uuid.uuid4().hex[:24]}",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            })
                        if tcd.get("id"):
                            self.tool_calls[idx]["id"] = tcd["id"]
                        if "function" in tcd:
                            fn = tcd["function"]
                            if fn.get("name"):
                                self.tool_calls[idx]["function"]["name"] += fn["name"]
                            if fn.get("arguments"):
                                self.tool_calls[idx]["function"]["arguments"] += fn["arguments"]
        finally:
            # Always flush whatever's still in the tag buffer before we emit
            # the final ``turn`` event. On cancellation (CancelledError raised
            # mid-iteration by ``/clear``, ``/exit``, or budget abort) this
            # would otherwise drop the last few sentences the model produced.
            self._flush_tag_buffer()

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
        }

    def _flush_tag_buffer(self) -> None:
        """Drain any pending characters held back for tag detection.

        At buffer-drain time we no longer have the option of seeing more
        characters, so partial tag prefixes (``<thi``, ``</thin``) are surfaced
        as plain text on whichever channel we're currently in. Better to show
        a stray ``<`` than to silently swallow real reply tail.
        """
        leftover = self._tag_buffer
        self._tag_buffer = ""
        if not leftover:
            return
        if self._in_reasoning:
            self.current_reasoning += leftover
            self._emit(leftover, reasoning=True)
        else:
            self.current_content += leftover
            self._emit(leftover, reasoning=False)

    def _emit(self, text: str, *, reasoning: bool) -> None:
        if not text:
            return
        self.server.emit(
            "turn",
            phase="reasoning" if reasoning else "text",
            delta=text,
            reasoningActive=bool(reasoning or self.current_reasoning),
        )

    def _coalesce_chunk(self, chunk: str, *, attr: str) -> str:
        """Trim provider-specific cumulative chunks down to only unseen text."""
        if not chunk:
            return ""

        previous = getattr(self, attr)
        # Some providers re-send the same full cumulative `content` as a second
        # delta; appending it again would double the user-visible text.
        if previous and chunk == previous:
            return ""
        if previous and len(chunk) > len(previous) and chunk.startswith(previous):
            setattr(self, attr, chunk)
            return chunk[len(previous):]

        setattr(self, attr, previous + chunk)
        return chunk
