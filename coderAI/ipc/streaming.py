"""Streaming handler that emits protocol events instead of Rich output.

Mirrors the contract of ``coderAI.ui.streaming.StreamingHandler`` but writes
``stream_delta`` NDJSON lines so the Ink UI can render deltas in React.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional


class IPCStreamingHandler:
    """Consume an LLM stream and relay deltas through an ``IPCServer``."""

    def __init__(self, server) -> None:
        self.server = server
        self.current_content = ""
        self.tool_calls: List[Dict[str, Any]] = []
        self._in_reasoning = False
        self._reasoning_type: Optional[str] = None
        self._tag_buffer = ""

    async def handle_stream(
        self, stream, initial_content: str = ""
    ) -> Dict[str, Any]:
        self.current_content = initial_content
        self.tool_calls = []
        self._in_reasoning = False
        self._reasoning_type = None
        self._tag_buffer = ""

        if initial_content:
            self._emit(initial_content, reasoning=False)

        async for chunk in stream:
            choices = chunk.get("choices", [])
            if not choices:
                continue
            delta = choices[0].get("delta", {})

            # Reasoning field (Anthropic / OpenAI o-series extensions)
            reasoning_delta = delta.get("reasoning_content", "") or ""
            if reasoning_delta:
                if not self._in_reasoning:
                    self._in_reasoning = True
                    self._reasoning_type = "field"
                self.current_content += reasoning_delta
                self._emit(reasoning_delta, reasoning=True)

            # Regular content (may contain <think> tags inline)
            content_chunk = delta.get("content", "") or ""

            if (
                content_chunk
                and self.current_content
                and len(content_chunk) > len(self.current_content)
                and content_chunk.startswith(self.current_content)
            ):
                content_chunk = content_chunk[len(self.current_content):]

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
                            self.current_content += before
                            self._emit(before, reasoning=True)
                            self._in_reasoning = False
                            self._reasoning_type = None
                            self._tag_buffer = after
                        else:
                            break

                if not self._in_reasoning:
                    last_open = self._tag_buffer.rfind("<")
                    if last_open != -1 and "<think>".startswith(
                        self._tag_buffer[last_open:]
                    ):
                        flush = self._tag_buffer[:last_open]
                        self._tag_buffer = self._tag_buffer[last_open:]
                    else:
                        flush = self._tag_buffer
                        self._tag_buffer = ""
                    if flush:
                        self.current_content += flush
                        self._emit(flush, reasoning=False)
                else:
                    last_open = self._tag_buffer.rfind("</")
                    if last_open == -1:
                        last_open = self._tag_buffer.rfind("<")
                    if last_open != -1 and "</think>".startswith(
                        self._tag_buffer[last_open:]
                    ):
                        flush = self._tag_buffer[:last_open]
                        self._tag_buffer = self._tag_buffer[last_open:]
                    else:
                        flush = self._tag_buffer
                        self._tag_buffer = ""
                    if flush:
                        self.current_content += flush
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

        return {
            "content": self.current_content,
            "tool_calls": self.tool_calls if self.tool_calls else None,
        }

    def _emit(self, text: str, *, reasoning: bool) -> None:
        if not text:
            return
        self.server.emit("stream_delta", content=text, reasoning=reasoning)
