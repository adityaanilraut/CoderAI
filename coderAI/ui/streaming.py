"""Streaming response handler for real-time display."""

import json
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
        current_tool_call = None

        # Start live display
        with Live(
            Markdown(self.current_content or "_Thinking..._"),
            refresh_per_second=10,
            console=display.console,
        ) as live:
            self.live = live

            async for chunk in stream:
                delta = chunk.get("choices", [{}])[0].get("delta", {})

                # Handle content
                if "content" in delta and delta["content"]:
                    self.current_content += delta["content"]
                    live.update(Markdown(self.current_content))

                # Handle tool calls
                if "tool_calls" in delta:
                    for tool_call_delta in delta["tool_calls"]:
                        index = tool_call_delta.get("index", 0)

                        # Initialize new tool call
                        while len(self.tool_calls) <= index:
                            self.tool_calls.append(
                                {
                                    "id": "",
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                }
                            )

                        # Update tool call
                        if "id" in tool_call_delta:
                            self.tool_calls[index]["id"] = tool_call_delta["id"]

                        if "function" in tool_call_delta:
                            func = tool_call_delta["function"]
                            if "name" in func:
                                self.tool_calls[index]["function"]["name"] += func["name"]
                            if "arguments" in func:
                                self.tool_calls[index]["function"]["arguments"] += func[
                                    "arguments"
                                ]

                # Check for finish
                finish_reason = chunk.get("choices", [{}])[0].get("finish_reason")
                if finish_reason:
                    break

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

