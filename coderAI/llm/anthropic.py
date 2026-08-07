"""Anthropic/Claude LLM provider implementation."""

import json
import logging
import ssl
from typing import Any, Optional
from collections.abc import AsyncIterator

import aiohttp

from coderAI.llm.base import (
    LLMProvider,
    REASONING_BUDGET_MAP,
    HTTP_CONNECT_TIMEOUT,
    HTTP_SOCK_READ_TIMEOUT,
    HTTP_TOTAL_TIMEOUT,
)
from coderAI.system.redaction import redact_text
from coderAI.system.retry import retry_async as _retry

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
# Derived from registry — only current 3-tier models support caching.
CACHING_SUPPORTED_MODELS = frozenset(
    {
        "claude-fable-5",
        "claude-opus-5",
        "claude-sonnet-5",
    }
)

# Friendly-name → API model ID. Single source is registry; this dict is kept
# for import-compat and mirrors registry aliases for the 3-tier catalog.
MODEL_ALIASES = {
    "claude-5-fable": "claude-fable-5",
    "claude-5-opus": "claude-opus-5",
    "claude-5-sonnet": "claude-sonnet-5",
    # Legacy short aliases resolve via registry LEGACY_ALIASES
    "fable": "claude-fable-5",
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
}

# Models that support adaptive extended thinking (all 3 current tiers).
_ADAPTIVE_THINKING_MODELS = frozenset(
    {
        "claude-fable-5",
        "claude-opus-5",
        "claude-sonnet-5",
    }
)
_MANUAL_THINKING_MODELS: frozenset[str] = frozenset()


def _is_anthropic_retryable(exc: Exception) -> bool:
    status = getattr(exc, "status", None)
    if isinstance(status, (int, float)):
        return int(status) in {429, 502, 503}
    msg = str(exc).lower()
    return any(
        kw in msg
        for kw in (
            "429",
            "502",
            "503",
            "rate limit",
            "too many requests",
            "service unavailable",
            "bad gateway",
            "server error",
        )
    )


class AnthropicProvider(LLMProvider):
    """Anthropic Claude LLM provider using the Messages API."""

    API_URL = "https://api.anthropic.com/v1/messages"
    API_VERSION = "2023-06-01"
    SUPPORTED_MODELS = list(MODEL_ALIASES.keys())
    # Anthropic replays the paused assistant tool_use blocks on the resumed
    # request, so the loop must keep them (see ExecutionLoop pause_turn path).
    preserves_tool_calls_on_pause = True
    MODEL_CONTEXT_WINDOWS = {
        "claude-fable-5": 1_000_000,
        "claude-opus-5": 1_000_000,
        "claude-sonnet-5": 1_000_000,
    }

    def __init__(self, model: str, api_key: Optional[str] = None, **kwargs: Any):
        """Initialize Anthropic provider.

        Args:
            model: Model name (claude-4-sonnet, claude-3.5-sonnet, etc.)
            api_key: Anthropic API key
            **kwargs: Additional options
        """
        super().__init__(model, api_key, **kwargs)
        from coderAI.llm.registry import resolve_alias as _resolve

        self.actual_model = _resolve(model)
        # keep short alias table for class-level compat
        self.actual_model = MODEL_ALIASES.get(self.actual_model, self.actual_model)
        self.total_cache_creation_tokens = 0
        self.total_cache_read_tokens = 0
        self._session: Optional[aiohttp.ClientSession] = None

        if not api_key:
            raise ValueError("Anthropic API key is required")

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create a reusable aiohttp session.

        Uses a shared connector with connection pooling (limit 20, 6/host),
        DNS cache, and cleanup of closed connections so parallel streaming
        and retry paths reuse TCP connections instead of opening a new
        socket per request.
        """
        if self._session is None or self._session.closed:
            ssl_ctx = _create_ssl_context()
            connector = aiohttp.TCPConnector(
                ssl=ssl_ctx,
                limit=20,
                limit_per_host=6,
                enable_cleanup_closed=True,
                ttl_dns_cache=300,
            )
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()

    def _get_headers(self) -> dict[str, str]:
        """Get API headers."""
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key or "",
            "anthropic-version": self.API_VERSION,
        }
        if self._supports_caching():
            headers["anthropic-beta"] = "prompt-caching-2024-07-31"
        return headers

    def _convert_messages(self, messages: list[dict[str, Any]]) -> tuple:
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
                    has_thinking = any(
                        (tc.get("provider_state") or {}).get("provider") == "anthropic"
                        and (tc.get("provider_state") or {}).get("thinking_blocks")
                        for tc in msg["tool_calls"]
                    )
                    if content and not has_thinking:
                        content_blocks.append({"type": "text", "text": content})
                    for tc in msg["tool_calls"]:
                        state = tc.get("provider_state") or {}
                        if state.get("provider") == "anthropic":
                            thinking_blocks = state.get("thinking_blocks")
                            if isinstance(thinking_blocks, list):
                                content_blocks.extend(dict(block) for block in thinking_blocks)
                                if content:
                                    content_blocks.append({"type": "text", "text": content})
                                    content = ""
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
                        content_blocks.append(
                            {
                                "type": "tool_use",
                                "id": tc.get("id", ""),
                                "name": func.get("name", ""),
                                "input": tool_input,
                            }
                        )
                    anthropic_messages.append({"role": "assistant", "content": content_blocks})
                else:
                    anthropic_messages.append({"role": "assistant", "content": content})
            elif role == "tool":
                # Convert tool results to Anthropic format.
                # Merge consecutive tool results into a single user message
                # (Anthropic requires alternating user/assistant roles).
                # Vision tool results (e.g. read_image) carry base64 images in
                # ``tool_images`` — render them as real image content blocks so
                # the model can actually see them.
                tool_images = msg.get("tool_images")
                result_content: Any = content
                if tool_images:
                    image_blocks: list[dict[str, Any]] = []
                    if content:
                        image_blocks.append({"type": "text", "text": content})
                    for img in tool_images:
                        data = img.get("data")
                        mime = img.get("mime_type")
                        if data and mime:
                            image_blocks.append(
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": mime,
                                        "data": data,
                                    },
                                }
                            )
                    if image_blocks:
                        result_content = image_blocks
                tool_result_block = {
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": result_content,
                }
                if (
                    anthropic_messages
                    and anthropic_messages[-1]["role"] == "user"
                    and isinstance(anthropic_messages[-1]["content"], list)
                ):
                    # Append to existing user message list of blocks
                    anthropic_messages[-1]["content"].append(tool_result_block)
                else:
                    anthropic_messages.append(
                        {
                            "role": "user",
                            "content": [tool_result_block],
                        }
                    )
            elif role == "user":
                # Anthropic requires strictly alternating user/assistant turns.
                # Merge into the previous user message when adjacent user turns occur.
                if anthropic_messages and anthropic_messages[-1]["role"] == "user":
                    prev_content = anthropic_messages[-1]["content"]
                    if isinstance(prev_content, list):
                        prev_content.append({"type": "text", "text": content})
                    else:
                        anthropic_messages[-1]["content"] = [
                            {"type": "text", "text": str(prev_content) + "\n"},
                            {"type": "text", "text": content},
                        ]
                else:
                    anthropic_messages.append(
                        {"role": "user", "content": [{"type": "text", "text": content}]}
                    )

        return system_prompt.strip(), anthropic_messages

    def _convert_tools(
        self, tools: Optional[list[dict[str, Any]]]
    ) -> Optional[list[dict[str, Any]]]:
        """Convert OpenAI tool format to Anthropic format."""
        if not tools:
            return None

        anthropic_tools = []
        for tool in tools:
            func = tool.get("function", {})
            anthropic_tools.append(
                {
                    "name": func.get("name", ""),
                    "description": func.get("description", ""),
                    "input_schema": func.get("parameters", {}),
                }
            )
        return anthropic_tools

    def _convert_response(self, response: dict[str, Any]) -> dict[str, Any]:
        """Convert Anthropic response to OpenAI-compatible format."""
        content_blocks = response.get("content", [])
        text_content = ""
        tool_calls = []
        pending_thinking = []

        for block in content_blocks:
            if block.get("type") in {"thinking", "redacted_thinking"}:
                pending_thinking.append(dict(block))
            elif block.get("type") == "text":
                text_content += block.get("text", "")
            elif block.get("type") == "tool_use":
                tool_call = {
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                }
                if pending_thinking:
                    tool_call["provider_state"] = {
                        "provider": "anthropic",
                        "thinking_blocks": pending_thinking,
                    }
                    pending_thinking = []
                tool_calls.append(tool_call)

        message: dict[str, Any] = {"content": text_content or None, "role": "assistant"}
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
        anthropic_messages: list[dict[str, Any]],
        anthropic_tools: Optional[list[dict[str, Any]]],
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
                content = [
                    {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
                ]
            else:
                content = [dict(b) for b in content]
                if content:
                    content[-1] = {**content[-1], "cache_control": {"type": "ephemeral"}}
            msg["content"] = content
            messages_payload[idx] = msg

        return system_payload, messages_payload, tools_payload

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        *,
        stream: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Build the Anthropic API payload.

        Returns the fully-assembled request body as a dictionary.
        """
        max_tokens = kwargs.get("max_tokens")
        system_prompt, anthropic_messages = self._convert_messages(messages)
        anthropic_tools = self._convert_tools(tools)

        payload: dict[str, Any] = {
            "model": self.actual_model,
            "max_tokens": max_tokens if max_tokens is not None else self.max_tokens,
        }
        if stream:
            payload["stream"] = True

        effort = kwargs.get("reasoning_effort", self.reasoning_effort)
        if self._supports_thinking() and effort and effort != "none":
            if self.actual_model in _ADAPTIVE_THINKING_MODELS:
                payload["thinking"] = {"type": "adaptive"}
                payload["output_config"] = {"effort": effort}
            else:
                # Manual thinking requires a budget smaller than max_tokens.
                budget = REASONING_BUDGET_MAP.get(effort, 8192)
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

    async def _post_to_anthropic(self, payload: dict[str, Any]) -> aiohttp.ClientResponse:
        session = await self._get_session()

        async def _do_post() -> Any:
            return await session.post(
                self.API_URL,
                headers=self._get_headers(),
                json=payload,
                timeout=aiohttp.ClientTimeout(
                    connect=HTTP_CONNECT_TIMEOUT,
                    sock_read=HTTP_SOCK_READ_TIMEOUT,
                    total=HTTP_TOTAL_TIMEOUT,
                ),
            )

        from typing import cast

        return cast(
            aiohttp.ClientResponse,
            await _retry(
                _do_post,
                description="Anthropic API",
                max_retries=3,
                is_retryable=_is_anthropic_retryable,
            ),
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send a chat request to Anthropic."""
        payload = self._build_payload(messages, tools, **kwargs)
        response = await self._post_to_anthropic(payload)
        async with response:
            if response.status != 200:
                error_body = redact_text(await response.text())
                raise RuntimeError(f"Anthropic API error {response.status}: {error_body[:200]}")
            result = await response.json()

            usage = result.get("usage", {})
            self.total_input_tokens += usage.get("input_tokens", 0)
            self.total_output_tokens += usage.get("output_tokens", 0)
            self.total_cache_creation_tokens += usage.get("cache_creation_input_tokens", 0)
            self.total_cache_read_tokens += usage.get("cache_read_input_tokens", 0)

            return self._convert_response(result)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream response from Anthropic (SSE-based)."""
        payload = self._build_payload(messages, tools, stream=True, **kwargs)
        response = await self._post_to_anthropic(payload)
        async with response:
            if response.status != 200:
                error_body = redact_text(await response.text())
                raise RuntimeError(f"Anthropic API error {response.status}: {error_body[:200]}")

            buffer = ""
            current_event = ""
            final_stop_reason = ""
            saw_message_stop = False
            # Per-call usage accumulated across streaming events so the terminal
            # message_stop chunk can surface it to the execution loop.
            call_input = 0
            call_output = 0
            call_cache_creation = 0
            call_cache_read = 0
            # State for reconstructing tool calls from streaming events
            tool_call_blocks: dict[int, dict[str, Any]] = {}  # index -> {id, name, arguments}
            thinking_blocks: dict[int, dict[str, Any]] = {}
            pending_thinking: list[dict[str, Any]] = []
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
                            inp = usage.get("input_tokens", 0)
                            cache_creation = usage.get("cache_creation_input_tokens", 0)
                            cache_read = usage.get("cache_read_input_tokens", 0)
                            self.total_input_tokens += inp
                            self.total_cache_creation_tokens += cache_creation
                            self.total_cache_read_tokens += cache_read
                            call_input += inp
                            call_cache_creation += cache_creation
                            call_cache_read += cache_read
                        elif current_event == "message_delta":
                            out = parsed.get("usage", {}).get("output_tokens", 0)
                            self.total_output_tokens += out
                            call_output += out
                            if "delta" in parsed and "stop_reason" in parsed["delta"]:
                                final_stop_reason = parsed["delta"]["stop_reason"]
                        elif current_event == "content_block_start":
                            block = parsed.get("content_block", {})
                            index = parsed.get("index", 0)
                            if block.get("type") in {"thinking", "redacted_thinking"}:
                                thinking_blocks[index] = dict(block)
                            elif block.get("type") == "tool_use":
                                tool_index = len(tool_call_blocks)
                                tool_call_blocks[index] = {
                                    "id": block.get("id", ""),
                                    "name": block.get("name", ""),
                                    "arguments": "",
                                    "tool_index": tool_index,
                                }
                                # Emit the tool call start in OpenAI format
                                yield {
                                    "choices": [
                                        {
                                            "delta": {
                                                "tool_calls": [
                                                    {
                                                        "index": tool_index,
                                                        "id": block.get("id", ""),
                                                        "type": "function",
                                                        "function": {
                                                            "name": block.get("name", ""),
                                                            "arguments": "",
                                                        },
                                                        **(
                                                            {
                                                                "provider_state": {
                                                                    "provider": "anthropic",
                                                                    "thinking_blocks": pending_thinking,
                                                                }
                                                            }
                                                            if pending_thinking
                                                            else {}
                                                        ),
                                                    }
                                                ]
                                            },
                                            "finish_reason": None,
                                        }
                                    ]
                                }
                                pending_thinking = []
                        elif current_event == "content_block_delta":
                            delta = parsed.get("delta", {})
                            index = parsed.get("index", 0)
                            if delta.get("type") == "text_delta":
                                yield {
                                    "choices": [
                                        {
                                            "delta": {"content": delta.get("text", "")},
                                            "finish_reason": None,
                                        }
                                    ]
                                }
                            elif delta.get("type") == "thinking_delta":
                                block = thinking_blocks.get(index)
                                if block is not None:
                                    block["thinking"] = block.get("thinking", "") + delta.get(
                                        "thinking", ""
                                    )
                                yield {
                                    "choices": [
                                        {
                                            "delta": {
                                                "reasoning_content": delta.get("thinking", "")
                                            },
                                            "finish_reason": None,
                                        }
                                    ]
                                }
                            elif delta.get("type") == "signature_delta":
                                block = thinking_blocks.get(index)
                                if block is not None:
                                    block["signature"] = block.get("signature", "") + delta.get(
                                        "signature", ""
                                    )
                            elif delta.get("type") == "input_json_delta":
                                partial_json = delta.get("partial_json", "")
                                if index in tool_call_blocks:
                                    cur = tool_call_blocks[index].get("arguments", "")
                                    # Cap per-tool-call JSON buffer (same limit as UI handler)
                                    _cap = 2_000_000
                                    if len(cur) < _cap:
                                        remaining = _cap - len(cur)
                                        if len(partial_json) > remaining:
                                            partial_json = (
                                                partial_json[:remaining]
                                                + " …[stream argument truncated]"
                                            )
                                            tool_call_blocks[index]["_overflow"] = True
                                        tool_call_blocks[index]["arguments"] = cur + partial_json
                                    else:
                                        tool_call_blocks[index]["_overflow"] = True
                                # Emit the argument chunk in OpenAI format
                                block_info = tool_call_blocks.get(index, {})
                                yield {
                                    "choices": [
                                        {
                                            "delta": {
                                                "tool_calls": [
                                                    {
                                                        "index": block_info.get("tool_index", 0),
                                                        "id": block_info.get("id", ""),
                                                        "type": "function",
                                                        "function": {
                                                            "name": "",
                                                            "arguments": partial_json,
                                                        },
                                                    }
                                                ]
                                            },
                                            "finish_reason": None,
                                        }
                                    ]
                                }
                        elif current_event == "content_block_stop":
                            index = parsed.get("index", 0)
                            block = thinking_blocks.pop(index, None)
                            if block is not None:
                                pending_thinking.append(block)
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
                            elif final_stop_reason == "max_tokens":
                                final_reason = "length"
                            else:
                                final_reason = "stop"
                            yield {
                                "choices": [
                                    {
                                        "delta": {},
                                        "finish_reason": final_reason,
                                    }
                                ],
                                "usage": {
                                    "input_tokens": call_input,
                                    "output_tokens": call_output,
                                    "cache_creation_input_tokens": call_cache_creation,
                                    "cache_read_input_tokens": call_cache_read,
                                },
                            }
            if not saw_message_stop:
                logger.warning(
                    "Anthropic stream ended without message_stop; partial tool buffers=%s",
                    {idx: block.get("arguments", "") for idx, block in tool_call_blocks.items()},
                )

    def _supports_thinking(self) -> bool:
        return self.actual_model in _ADAPTIVE_THINKING_MODELS | _MANUAL_THINKING_MODELS

    def count_tokens(self, text: str) -> int:
        """Approximate token count for Claude models.

        Claude uses about 1 token per 4 characters on average.
        """
        from coderAI.llm._token_counter import count_tokens_anthropic

        return count_tokens_anthropic(text, self.actual_model, self.api_key)

    def supports_tools(self) -> bool:
        """Claude supports tool calling."""
        return True

    def get_cost(self) -> dict[str, Any]:
        """Get current session cost estimate."""
        from coderAI.system.cost import CostTracker

        pricing = CostTracker.get_model_pricing(self.actual_model)
        # MODEL_PRICING is per-million tokens.
        input_per_token = pricing["input"] / 1_000_000
        output_per_token = pricing["output"] / 1_000_000
        # Cache write costs 1.25x input; cache read costs 0.1x input.
        cache_write_cost = self.total_cache_creation_tokens * input_per_token * 1.25
        cache_read_cost = self.total_cache_read_tokens * input_per_token * 0.1
        uncached_input = max(
            0,
            self.total_input_tokens
            - self.total_cache_creation_tokens
            - self.total_cache_read_tokens,
        )
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

    def clean_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep Anthropic's opaque tool-call state for message conversion."""
        return messages

    def get_model_info(self) -> dict[str, Any]:
        info = super().get_model_info()
        info["provider"] = "anthropic"
        info["cache_creation_tokens"] = self.total_cache_creation_tokens
        info["cache_read_tokens"] = self.total_cache_read_tokens
        return info
