"""Anthropic/Claude LLM provider implementation."""

import json
import logging
import ssl
from typing import Any, AsyncIterator, Dict, List, Optional

import aiohttp

from .base import LLMProvider, REASONING_BUDGET_MAP

logger = logging.getLogger(__name__)


def _create_ssl_context() -> ssl.SSLContext:
    """Create an SSL context using certifi's CA bundle.

    This is required on macOS with Python 3.13 where the system SSL
    certificates are often not configured for the framework Python install.
    Falls back to the default context if certifi is unavailable.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()

# Models that support prompt caching (cache_control on system/tools/messages).
CACHING_SUPPORTED_MODELS = frozenset({
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
    "claude-3-7-sonnet-20250219",
    "claude-3-5-sonnet-20241022",
    "claude-3-5-haiku-20241022",
    "claude-3-opus-20240229",
})

# Friendly-name → API model ID.
# Update the right-hand side when the API retires a dated snapshot.
MODEL_ALIASES = {
    # Claude 4.X (current)
    "claude-4.7-opus": "claude-opus-4-7",
    "claude-4.6-sonnet": "claude-sonnet-4-6",
    "claude-4.5-haiku": "claude-haiku-4-5-20251001",
    # Kept for back-compat with existing configs (default_model was
    # "claude-4-sonnet"). Point at the current 4.6 sonnet.
    "claude-4-sonnet": "claude-sonnet-4-6",
    "claude-4-opus": "claude-opus-4-7",
    "claude-4-haiku": "claude-haiku-4-5-20251001",
    # Legacy 3.X snapshots (retained so existing sessions still resolve).
    "claude-3.7-sonnet": "claude-3-7-sonnet-20250219",
    "claude-3.5-sonnet": "claude-3-5-sonnet-20241022",
    "claude-3.5-haiku": "claude-3-5-haiku-20241022",
    "claude-3-opus": "claude-3-opus-20240229",
    # Short aliases: always resolve to the newest of each tier.
    "opus": "claude-opus-4-7",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}

# Models that support extended thinking with a token budget.
_THINKING_SUPPORTED_MODELS = frozenset({
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-3-7-sonnet-20250219",
})


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
        self.max_tokens = kwargs.get("max_tokens", 8192)
        self.reasoning_effort = kwargs.get("reasoning_effort", "medium")
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cache_creation_tokens = 0
        self.total_cache_read_tokens = 0
        self._session: Optional[aiohttp.ClientSession] = None

        if not api_key:
            raise ValueError("Anthropic API key is required")

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create a reusable aiohttp session."""
        if self._session is None or self._session.closed:
            ssl_ctx = _create_ssl_context()
            connector = aiohttp.TCPConnector(ssl=ssl_ctx)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()

    def _get_headers(self) -> Dict[str, str]:
        """Get API headers."""
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": self.API_VERSION,
        }
        if self._supports_caching():
            headers["anthropic-beta"] = "prompt-caching-2024-07-31"
        return headers

    def _convert_messages(self, messages: List[Dict[str, Any]]) -> tuple:
        """Convert OpenAI-style messages to Anthropic format.

        Returns:
            Tuple of (system_prompt, anthropic_messages)
        """
        system_prompt = ""
        anthropic_messages = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content")
            if content is None:
                content = ""

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
                        raw_args = func.get("arguments", "{}")
                        try:
                            tool_input = json.loads(raw_args)
                        except json.JSONDecodeError as e:
                            # Malformed JSON in stored tool args is unrecoverable
                            # here: silently substituting ``{}`` would invoke the
                            # tool with empty arguments on replay (e.g. a
                            # ``git_reset`` with default ``reset_type='hard'``).
                            # Raise so the agent loop can surface the error and
                            # the model can try again with valid args.
                            raise ValueError(
                                f"Could not parse tool arguments for "
                                f"{func.get('name', '?')!r}: {e}. Raw: {raw_args!r}"
                            ) from e
                        if not isinstance(tool_input, dict):
                            raise ValueError(
                                f"Tool arguments for {func.get('name', '?')!r} "
                                f"must decode to a JSON object, got "
                                f"{type(tool_input).__name__}"
                            )
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
                # Anthropic requires strictly alternating user/assistant turns.
                # Merge into the previous user message when adjacent user turns occur
                # (e.g. injected error messages, pinned-context inserts, tool results
                # followed by a recovery prompt).
                if anthropic_messages and anthropic_messages[-1]["role"] == "user":
                    prev_content = anthropic_messages[-1]["content"]
                    if isinstance(prev_content, list):
                        prev_content.append({"type": "text", "text": content or ""})
                    else:
                        anthropic_messages[-1]["content"] = (prev_content or "") + "\n" + (content or "")
                else:
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
        if stop_reason == "tool_use":
            finish_reason = "tool_calls"
        elif stop_reason == "end_turn" and not tool_calls:
            finish_reason = "stop"
        elif stop_reason == "max_tokens":
            finish_reason = "length"
        elif stop_reason == "refusal":
            finish_reason = "refusal"
        elif stop_reason == "pause_turn":
            finish_reason = "pause_turn"

        return {
            "choices": [{"message": message, "finish_reason": finish_reason}],
            "usage": response.get("usage", {}),
        }

    def _supports_caching(self) -> bool:
        """Check if the current model supports prompt caching."""
        return self.actual_model in CACHING_SUPPORTED_MODELS

    def _apply_cache_control(
        self,
        system_prompt: str,
        anthropic_messages: List[Dict[str, Any]],
        anthropic_tools: Optional[List[Dict[str, Any]]],
    ) -> tuple:
        """Add cache_control breakpoints to system, tools, and message history.

        Strategy:
        - System prompt → single content block marked ephemeral (cached every request)
        - Tools → last tool marked ephemeral (tool list rarely changes)
        - Messages → penultimate user message marked ephemeral (caches growing history;
          the final user message is always new and intentionally left uncached)

        Returns (system_payload, messages_payload, tools_payload).
        """
        system_payload = None
        if system_prompt:
            system_payload = [
                {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}
            ]

        tools_payload = None
        if anthropic_tools:
            tools_payload = [dict(t) for t in anthropic_tools]
            tools_payload[-1] = {**tools_payload[-1], "cache_control": {"type": "ephemeral"}}

        messages_payload = [dict(m) for m in anthropic_messages]
        user_indices = [i for i, m in enumerate(messages_payload) if m["role"] == "user"]
        if len(user_indices) >= 2:
            idx = user_indices[-2]
            msg = dict(messages_payload[idx])
            content = msg["content"]
            if isinstance(content, str):
                content = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
            else:
                content = [dict(b) for b in content]
                if content:
                    content[-1] = {**content[-1], "cache_control": {"type": "ephemeral"}}
            msg["content"] = content
            messages_payload[idx] = msg

        return system_payload, messages_payload, tools_payload

    def _build_payload(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        max_tokens: Optional[int] = None,
        stream: bool = False,
    ) -> Dict[str, Any]:
        """Build the Anthropic API payload.

        Returns the fully-assembled request body as a dictionary.
        """
        system_prompt, anthropic_messages = self._convert_messages(messages)
        anthropic_tools = self._convert_tools(tools)

        payload: Dict[str, Any] = {
            "model": self.actual_model,
            "max_tokens": max_tokens if max_tokens is not None else self.max_tokens,
        }
        if stream:
            payload["stream"] = True

        if self._supports_thinking() and self.reasoning_effort and self.reasoning_effort != "none":
            # Per Anthropic's extended-thinking API:
            # {"type": "enabled", "budget_tokens": N} where N < max_tokens.
            budget = REASONING_BUDGET_MAP.get(self.reasoning_effort, 8192)
            request_max = max_tokens if max_tokens is not None else self.max_tokens
            if budget >= request_max:
                budget = max(1024, request_max - 1024)
                if budget >= request_max:
                    budget = request_max - 1
            payload["thinking"] = {"type": "enabled", "budget_tokens": budget}

        if self._supports_caching():
            sys_p, msg_p, tool_p = self._apply_cache_control(
                system_prompt, anthropic_messages, anthropic_tools
            )
            if sys_p:
                payload["system"] = sys_p
            if tool_p:
                payload["tools"] = tool_p
            payload["messages"] = msg_p
        else:
            if system_prompt:
                payload["system"] = system_prompt
            if anthropic_tools:
                payload["tools"] = anthropic_tools
            payload["messages"] = anthropic_messages

        return payload

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Send a chat request to Anthropic."""
        payload = self._build_payload(messages, tools, max_tokens=kwargs.get("max_tokens"))
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
                    f"Anthropic API error {response.status}: {error_body[:200]}"
                )
            result = await response.json()

            usage = result.get("usage", {})
            self.total_input_tokens += usage.get("input_tokens", 0)
            self.total_output_tokens += usage.get("output_tokens", 0)
            self.total_cache_creation_tokens += usage.get("cache_creation_input_tokens", 0)
            self.total_cache_read_tokens += usage.get("cache_read_input_tokens", 0)

            return self._convert_response(result)

    async def stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Stream response from Anthropic (SSE-based)."""
        payload = self._build_payload(messages, tools, max_tokens=kwargs.get("max_tokens"), stream=True)
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
                    f"Anthropic API error {response.status}: {error_body[:200]}"
                )

            buffer = ""
            current_event = ""
            final_stop_reason = ""
            saw_message_stop = False
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
                        if current_event == "message_start":
                            usage = parsed.get("message", {}).get("usage", {})
                            self.total_input_tokens += usage.get("input_tokens", 0)
                            self.total_cache_creation_tokens += usage.get("cache_creation_input_tokens", 0)
                            self.total_cache_read_tokens += usage.get("cache_read_input_tokens", 0)
                        elif current_event == "message_delta":
                            self.total_output_tokens += parsed.get("usage", {}).get("output_tokens", 0)
                            if "delta" in parsed and "stop_reason" in parsed["delta"]:
                                final_stop_reason = parsed["delta"]["stop_reason"]
                        elif current_event == "content_block_start":
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
                                block_info = tool_call_blocks.get(index, {})
                                yield {
                                    "choices": [{
                                        "delta": {
                                            "tool_calls": [{
                                                "index": index,
                                                "id": block_info.get("id", ""),
                                                "type": "function",
                                                "function": {
                                                    "name": block_info.get("name", ""),
                                                    "arguments": partial_json,
                                                },
                                            }]
                                        },
                                        "finish_reason": None,
                                    }]
                                }
                        elif current_event == "message_stop":
                            saw_message_stop = True
                            # Use 'tool_calls' if any tool_use blocks were seen,
                            # matching the non-streaming _convert_response() behavior.
                            if tool_call_blocks:
                                final_reason = "tool_calls"
                            elif final_stop_reason == "refusal":
                                final_reason = "refusal"
                            elif final_stop_reason == "pause_turn":
                                final_reason = "pause_turn"
                            else:
                                final_reason = "stop"
                            yield {
                                "choices": [{
                                    "delta": {},
                                    "finish_reason": final_reason,
                                }]
                            }
            if not saw_message_stop:
                logger.warning(
                    "Anthropic stream ended without message_stop; partial tool buffers=%s",
                    {idx: block.get("arguments", "") for idx, block in tool_call_blocks.items()},
                )

    def _supports_thinking(self) -> bool:
        return self.actual_model in _THINKING_SUPPORTED_MODELS

    def count_tokens(self, text: str) -> int:
        """Approximate token count for Claude models.

        Claude uses about 1 token per 4 characters on average.
        """
        from ._token_counter import count_tokens_anthropic
        return count_tokens_anthropic(text, self.actual_model, self.api_key)

    def supports_tools(self) -> bool:
        """Claude supports tool calling."""
        return True

    def get_cost(self) -> Dict[str, Any]:
        """Get current session cost estimate."""
        from ..cost import CostTracker
        pricing = CostTracker.get_model_pricing(self.actual_model)
        # MODEL_PRICING is per-million tokens.
        input_per_token = pricing["input"] / 1_000_000
        output_per_token = pricing["output"] / 1_000_000
        # Cache write costs 1.25x input; cache read costs 0.1x input.
        cache_write_cost = self.total_cache_creation_tokens * input_per_token * 1.25
        cache_read_cost = self.total_cache_read_tokens * input_per_token * 0.1
        uncached_input = max(0, self.total_input_tokens - self.total_cache_creation_tokens - self.total_cache_read_tokens)
        uncached_input_cost = uncached_input * input_per_token
        output_cost = self.total_output_tokens * output_per_token
        total_cost = uncached_input_cost + output_cost + cache_write_cost + cache_read_cost

        return {
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "cache_creation_tokens": self.total_cache_creation_tokens,
            "cache_read_tokens": self.total_cache_read_tokens,
            "total_tokens": self.total_input_tokens + self.total_output_tokens,
            "input_cost": round(uncached_input_cost, 6),
            "output_cost": round(output_cost, 6),
            "cache_write_cost": round(cache_write_cost, 6),
            "cache_read_cost": round(cache_read_cost, 6),
            "total_cost": round(total_cost, 6),
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
        info["cache_creation_tokens"] = self.total_cache_creation_tokens
        info["cache_read_tokens"] = self.total_cache_read_tokens
        return info
