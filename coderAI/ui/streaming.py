"""Streaming response handler for real-time display."""

import json
import uuid
from typing import Any, Dict, List, Optional

from rich.live import Live
from rich.markdown import Markdown

from .display import display


class StreamingHandler:
    """Handles streaming LLM responses with live updates."""

    def __init__(self):
        """Initialize streaming handler."""
        self.current_content = ""
        self.tool_calls = []
        self.live: Optional[Live] = None

    async def handle_stream(self, stream, initial_content: str = ""):
        """Handle a streaming response.

        Args:
            stream: Async iterator of response chunks
            initial_content: Initial content to display

        Returns:
            Dictionary with complete response
        """
        self.current_content = initial_content
        self.tool_calls = []
        self.in_reasoning = False
        self.reasoning_type = None
        self._tag_buffer = ""

        # Start live display
        with Live(
            Markdown(self.current_content or "_Thinking..._"),
            refresh_per_second=10,
            console=display.console,
        ) as live:
            self.live = live

            async for chunk in stream:
                choices = chunk.get("choices", [])
                if not choices:
                    continue  # Skip chunks with no choices (e.g. usage-only chunks)
                delta = choices[0].get("delta", {})

                # Handle reasoning content from API explicitly
                reasoning_delta = delta.get("reasoning_content", "")
                if reasoning_delta:
                    if not self.in_reasoning:
                        self.in_reasoning = True
                        self.reasoning_type = "field"
                        if self.current_content and not self.current_content.endswith("\n"):
                            self.current_content += "\n\n"
                        self.current_content += "> _Thinking..._\n> \n> "
                    
                    self.current_content += reasoning_delta.replace("\n", "\n> ")
                    live.update(Markdown(self.current_content))

                # Handle regular content (which might contain <think> tags)
                content_chunk = delta.get("content", "")
                if content_chunk:
                    # If field-based reasoning transitions to content
                    if self.in_reasoning and self.reasoning_type == "field":
                        self.current_content += "\n\n"
                        self.in_reasoning = False
                        self.reasoning_type = None

                    self._tag_buffer += content_chunk
                    
                    # Process the buffer for complete tags
                    while True:
                        if not self.in_reasoning:
                            if "<think>" in self._tag_buffer:
                                before, after = self._tag_buffer.split("<think>", 1)
                                self.current_content += before
                                
                                self.in_reasoning = True
                                self.reasoning_type = "tag"
                                if self.current_content and not self.current_content.endswith("\n"):
                                    self.current_content += "\n\n"
                                self.current_content += "> _Thinking..._\n> \n> "
                                self._tag_buffer = after
                            else:
                                break
                        else:
                            if "</think>" in self._tag_buffer:
                                before, after = self._tag_buffer.split("</think>", 1)
                                self.current_content += before.replace("\n", "\n> ")
                                
                                self.in_reasoning = False
                                self.reasoning_type = None
                                self.current_content += "\n\n"
                                self._tag_buffer = after
                            else:
                                break

                    # Flush safe parts of the buffer
                    # If buffer looks like it might be forming a tag, hold it.
                    # Max tag length we care about is len("</think>") == 8
                    if not self.in_reasoning:
                        # Look for incomplete '<think>' starting tag
                        last_open = self._tag_buffer.rfind("<")
                        if last_open != -1 and "<think>".startswith(self._tag_buffer[last_open:]):
                            flush_part = self._tag_buffer[:last_open]
                            self._tag_buffer = self._tag_buffer[last_open:]
                        else:
                            flush_part = self._tag_buffer
                            self._tag_buffer = ""
                        if flush_part:
                            self.current_content += flush_part
                    else:
                        # Look for incomplete '</think>' ending tag
                        last_open = self._tag_buffer.rfind("</")
                        if last_open == -1:
                            last_open = self._tag_buffer.rfind("<")
                            
                        if last_open != -1 and "</think>".startswith(self._tag_buffer[last_open:]):
                            flush_part = self._tag_buffer[:last_open]
                            self._tag_buffer = self._tag_buffer[last_open:]
                        else:
                            flush_part = self._tag_buffer
                            self._tag_buffer = ""
                        if flush_part:
                            self.current_content += flush_part.replace("\n", "\n> ")

                    live.update(Markdown(self.current_content))

                # Handle tool calls
                if delta.get("tool_calls"):
                    for tool_call_delta in delta["tool_calls"]:
                        index = tool_call_delta.get("index", 0)

                        # Initialize new tool call
                        while len(self.tool_calls) <= index:
                            self.tool_calls.append(
                                {
                                    "id": f"call_{uuid.uuid4().hex[:24]}",
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                }
                            )

                        # Update tool call id only when the stream provides a real value
                        if tool_call_delta.get("id"):
                            self.tool_calls[index]["id"] = tool_call_delta["id"]

                        if "function" in tool_call_delta:
                            func = tool_call_delta["function"]
                            if func.get("name"):
                                self.tool_calls[index]["function"]["name"] += func["name"]
                            if func.get("arguments"):
                                self.tool_calls[index]["function"]["arguments"] += func[
                                    "arguments"
                                ]

                # Note: we let the stream exhaust naturally instead of breaking
                # on finish_reason, as some providers send additional chunks
                # after the finish_reason field is set.

        return {
            "content": self.current_content,
            "tool_calls": self.tool_calls if self.tool_calls else None,
        }

    def display_tool_calls(self):
        """Display parsed tool calls."""
        if not self.tool_calls:
            return

        display.print("\n")
        for tool_call in self.tool_calls:
            try:
                func_name = tool_call["function"]["name"]
                func_args = json.loads(tool_call["function"]["arguments"])
                display.print_tool_call(func_name, func_args)
            except Exception:
                continue


# Global streaming handler instance
streaming_handler = StreamingHandler()

