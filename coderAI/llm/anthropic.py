"""Anthropic/Claude LLM provider implementation."""

import json
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

import aiohttp

from .base import LLMProvider

logger = logging.getLogger(__name__)

# Cost per 1K tokens (approximate)
MODEL_COSTS = {
    "claude-sonnet-4-20250514": {"input": 0.003, "output": 0.015},
    "claude-3-5-sonnet-20241022": {"input": 0.003, "output": 0.015},
    "claude-3-5-haiku-20241022": {"input": 0.0008, "output": 0.004},
    "claude-3-opus-20240229": {"input": 0.015, "output": 0.075},
}

# Map friendly names to API model names
MODEL_ALIASES = {
    "claude-4-sonnet": "claude-sonnet-4-20250514",
    "claude-3.5-sonnet": "claude-3-5-sonnet-20241022",
    "claude-3.5-haiku": "claude-3-5-haiku-20241022",
    "claude-3-opus": "claude-3-opus-20240229",
}


class AnthropicProvider(LLMProvider):
    """Anthropic Claude LLM provider using the Messages API."""

    API_URL = "https://api.anthropic.com/v1/messages"
    API_VERSION = "2023-06-01"
    SUPPORTED_MODELS = list(MODEL_ALIASES.keys())

    def __init__(self, model: str, api_key: Optional[str] = None, **kwargs):
        """Initialize Anthropic provider.

        Args:
            model: Model name (claude-4-sonnet, claude-3.5-sonnet, etc.)
            api_key: Anthropic API key
            **kwargs: Additional options
        """
        super().__init__(model, api_key, **kwargs)
        self.actual_model = MODEL_ALIASES.get(model, model)
        self.temperature = kwargs.get("temperature", 0.7)
        self.max_tokens = kwargs.get("max_tokens", 4096)
        self.reasoning_effort = kwargs.get("reasoning_effort", "medium")
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self._session: Optional[aiohttp.ClientSession] = None

        if not api_key:
            raise ValueError("Anthropic API key is required")

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create a reusable aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()

    def __del__(self):
        """Best-effort cleanup of the HTTP session on garbage collection."""
        if self._session and not self._session.closed:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(self._session.close())
                else:
                    loop.run_until_complete(self._session.close())
            except Exception:
                pass

    def _get_headers(self) -> Dict[str, str]:
        """Get API headers."""
        return {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": self.API_VERSION,
        }

    def _convert_messages(self, messages: List[Dict[str, Any]]) -> tuple:
        """Convert OpenAI-style messages to Anthropic format.

        Returns:
            Tuple of (system_prompt, anthropic_messages)
        """
        system_prompt = ""
        anthropic_messages = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "system":
                system_prompt += content + "\n"
            elif role == "assistant":
                # Handle tool_calls — convert to Anthropic tool_use format
                if msg.get("tool_calls"):
                    content_blocks = []
                    if content:
                        content_blocks.append({"type": "text", "text": content})
                    for tc in msg["tool_calls"]:
                        func = tc.get("function", {})
                        try:
                            tool_input = json.loads(func.get("arguments", "{}"))
                        except json.JSONDecodeError:
                            tool_input = {}
                        content_blocks.append({
                            "type": "tool_use",
                            "id": tc.get("id", ""),
                            "name": func.get("name", ""),
                            "input": tool_input,
                        })
                    anthropic_messages.append({"role": "assistant", "content": content_blocks})
                else:
                    anthropic_messages.append({"role": "assistant", "content": content or ""})
            elif role == "tool":
                # Convert tool results to Anthropic format.
                # Merge consecutive tool results into a single user message
                # (Anthropic requires alternating user/assistant roles).
                tool_result_block = {
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": content,
                }
                if (
                    anthropic_messages
                    and anthropic_messages[-1]["role"] == "user"
                    and isinstance(anthropic_messages[-1]["content"], list)
                    and anthropic_messages[-1]["content"]
                    and anthropic_messages[-1]["content"][0].get("type") == "tool_result"
                ):
                    # Append to existing user message
                    anthropic_messages[-1]["content"].append(tool_result_block)
                else:
                    anthropic_messages.append({
                        "role": "user",
                        "content": [tool_result_block],
                    })
            elif role == "user":
                anthropic_messages.append({"role": "user", "content": content})

        return system_prompt.strip(), anthropic_messages

    def _convert_tools(self, tools: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
        """Convert OpenAI tool format to Anthropic format."""
        if not tools:
            return None

        anthropic_tools = []
        for tool in tools:
            func = tool.get("function", {})
            anthropic_tools.append({
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {}),
            })
        return anthropic_tools

    def _convert_response(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """Convert Anthropic response to OpenAI-compatible format."""
        content_blocks = response.get("content", [])
        text_content = ""
        tool_calls = []

        for block in content_blocks:
            if block.get("type") == "text":
                text_content += block.get("text", "")
            elif block.get("type") == "tool_use":
                tool_calls.append({
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                })

        message = {"content": text_content or None, "role": "assistant"}
        if tool_calls:
            message["tool_calls"] = tool_calls

        finish_reason = "tool_calls" if tool_calls else "stop"
        stop_reason = response.get("stop_reason", "")
        if stop_reason == "end_turn":
            finish_reason = "stop"

        return {
            "choices": [{"message": message, "finish_reason": finish_reason}],
            "usage": response.get("usage", {}),
        }

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Send a chat request to Anthropic with retry logic."""
        system_prompt, anthropic_messages = self._convert_messages(messages)
        anthropic_tools = self._convert_tools(tools)

        payload = {
            "model": self.actual_model,
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "messages": anthropic_messages,
        }
        
        if self._supports_thinking() and self.reasoning_effort and self.reasoning_effort != "none":
            budget = {"low": 1024, "medium": 4096, "high": 16384}.get(self.reasoning_effort, 4096)
            payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
            
        if system_prompt:
            payload["system"] = system_prompt
        if anthropic_tools:
            payload["tools"] = anthropic_tools

        session = await self._get_session()
        async with session.post(
            self.API_URL,
            headers=self._get_headers(),
            json=payload,
            timeout=aiohttp.ClientTimeout(total=120),
        ) as response:
            if response.status != 200:
                error_body = await response.text()
                raise RuntimeError(
                    f"Anthropic API error {response.status}: {error_body}"
                )
            result = await response.json()

            # Track usage
            usage = result.get("usage", {})
            self.total_input_tokens += usage.get("input_tokens", 0)
            self.total_output_tokens += usage.get("output_tokens", 0)

            return self._convert_response(result)

    async def stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Stream response from Anthropic (SSE-based)."""
        system_prompt, anthropic_messages = self._convert_messages(messages)
        anthropic_tools = self._convert_tools(tools)

        payload = {
            "model": self.actual_model,
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "messages": anthropic_messages,
            "stream": True,
        }
        
        if self._supports_thinking() and self.reasoning_effort and self.reasoning_effort != "none":
            budget = {"low": 1024, "medium": 4096, "high": 16384}.get(self.reasoning_effort, 4096)
            payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
            
        if system_prompt:
            payload["system"] = system_prompt
        if anthropic_tools:
            payload["tools"] = anthropic_tools

        session = await self._get_session()
        async with session.post(
            self.API_URL,
            headers=self._get_headers(),
            json=payload,
            timeout=aiohttp.ClientTimeout(total=120),
        ) as response:
            if response.status != 200:
                error_body = await response.text()
                raise RuntimeError(
                    f"Anthropic API error {response.status}: {error_body}"
                )

            buffer = ""
            current_event = ""
            # State for reconstructing tool calls from streaming events
            tool_call_blocks: Dict[int, Dict[str, Any]] = {}  # index -> {id, name, arguments}
            async for raw_chunk in response.content:
                buffer += raw_chunk.decode("utf-8")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("event: "):
                        current_event = line[7:]
                    elif line.startswith("data: "):
                        data = line[6:]
                        try:
                            parsed = json.loads(data)
                        except json.JSONDecodeError:
                            continue

                        # Convert Anthropic streaming events to OpenAI chunk format
                        if current_event == "content_block_start":
                            block = parsed.get("content_block", {})
                            index = parsed.get("index", 0)
                            if block.get("type") == "tool_use":
                                tool_call_blocks[index] = {
                                    "id": block.get("id", ""),
                                    "name": block.get("name", ""),
                                    "arguments": "",
                                }
                                # Emit the tool call start in OpenAI format
                                yield {
                                    "choices": [{
                                        "delta": {
                                            "tool_calls": [{
                                                "index": index,
                                                "id": block.get("id", ""),
                                                "type": "function",
                                                "function": {
                                                    "name": block.get("name", ""),
                                                    "arguments": "",
                                                },
                                            }]
                                        },
                                        "finish_reason": None,
                                    }]
                                }
                        elif current_event == "content_block_delta":
                            delta = parsed.get("delta", {})
                            index = parsed.get("index", 0)
                            if delta.get("type") == "text_delta":
                                yield {
                                    "choices": [{
                                        "delta": {"content": delta.get("text", "")},
                                        "finish_reason": None,
                                    }]
                                }
                            elif delta.get("type") == "thinking_delta":
                                yield {
                                    "choices": [{
                                        "delta": {"reasoning_content": delta.get("thinking", "")},
                                        "finish_reason": None,
                                    }]
                                }
                            elif delta.get("type") == "input_json_delta":
                                partial_json = delta.get("partial_json", "")
                                if index in tool_call_blocks:
                                    tool_call_blocks[index]["arguments"] += partial_json
                                # Emit the argument chunk in OpenAI format
                                yield {
                                    "choices": [{
                                        "delta": {
                                            "tool_calls": [{
                                                "index": index,
                                                "function": {
                                                    "arguments": partial_json,
                                                },
                                            }]
                                        },
                                        "finish_reason": None,
                                    }]
                                }
                        elif current_event == "message_stop":
                            yield {
                                "choices": [{
                                    "delta": {},
                                    "finish_reason": "stop",
                                }]
                            }

    def _supports_thinking(self) -> bool:
        """Check if the current model supports extended thinking."""
        # Claude 3.7+ and Claude 4+ models support extended thinking
        model = self.actual_model
        return any(tag in model for tag in ("claude-3-7", "claude-4", "claude-sonnet-4"))

    def count_tokens(self, text: str) -> int:
        """Approximate token count for Claude models.

        Claude uses about 1 token per 4 characters on average.
        """
        return max(1, len(text) // 4)

    def supports_tools(self) -> bool:
        """Claude supports tool calling."""
        return True

    def get_cost(self) -> Dict[str, Any]:
        """Get current session cost estimate."""
        costs = MODEL_COSTS.get(self.actual_model, {"input": 0, "output": 0})
        input_cost = (self.total_input_tokens / 1000) * costs["input"]
        output_cost = (self.total_output_tokens / 1000) * costs["output"]

        return {
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "total_tokens": self.total_input_tokens + self.total_output_tokens,
            "input_cost": round(input_cost, 6),
            "output_cost": round(output_cost, 6),
            "total_cost": round(input_cost + output_cost, 6),
            "currency": "USD",
            "model": self.actual_model,
        }

    def get_model_info(self) -> Dict[str, Any]:
        """Get information about the current model."""
        info = super().get_model_info()
        info["provider"] = "anthropic"
        info["cost"] = self.get_cost()
        info["total_input_tokens"] = self.total_input_tokens
        info["total_output_tokens"] = self.total_output_tokens
        info["total_tokens"] = self.total_input_tokens + self.total_output_tokens
        return info
