"""Provider / model capability vocabulary and LLM constructor factories.

Features:
- ProviderType and ModelCapability literals and derivation
- ChatProvider factories (Anthropic, OpenAI Legacy/Responses, Google GenAI/Gemini, Moonshot, Echo, Chaos)
- Request-scoped token estimators and max completion token computation
- Preserved CoderAI client pool and OpenAI client compatibility
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, Self, cast, get_args

from kosong.chat_provider import ChatProvider, StreamedMessage, ThinkingEffort
from kosong.message import (
    AudioURLPart,
    ImageURLPart,
    Message,
    TextPart,
    ThinkPart,
    VideoURLPart,
)
from kosong.tooling import Tool
from kosong.utils.aio import Callback, callback
from pydantic import SecretStr

from coderai.constant import USER_AGENT
from coderai.utils.logging import logger

if TYPE_CHECKING:
    from kosong.chat_provider.kimi import Kimi


ProviderType = Literal[
    "kimi",
    "openai_legacy",
    "openai_responses",
    "anthropic",
    "google_genai",  # backward-compat alias for ``gemini``
    "gemini",
    "vertexai",
    "_echo",
    "_scripted_echo",
    "_chaos",
]

ModelCapability = Literal["image_in", "video_in", "thinking", "always_thinking"]

ALL_MODEL_CAPABILITIES: set[ModelCapability] = set(get_args(ModelCapability))

#: Token budget reserved per attached media item when estimating requests.
MEDIA_TOKEN_ESTIMATE = 2_000

#: Fallback completion budget when the context size is unknown.
DEFAULT_UNKNOWN_CONTEXT_COMPLETION_TOKENS = 32_000

#: Safety margin subtracted when deriving completion limits.
DEFAULT_COMPLETION_TOKEN_SAFETY_MARGIN = 1_024


@dataclass(slots=True)
class LLM:
    chat_provider: ChatProvider
    max_context_size: int
    capabilities: set[ModelCapability]
    model_config: Any | None = None
    provider_config: Any | None = None

    @property
    def model_name(self) -> str:
        return self.chat_provider.model_name


class _GenerationOverrideProvider(Protocol):
    async def generate(
        self,
        system_prompt: str,
        tools: Sequence[Tool],
        history: Sequence[Message],
        *,
        generation_overrides: Mapping[str, Any] | None = None,
    ) -> StreamedMessage: ...


@dataclass(slots=True)
class _KimiRequestChatProvider:
    """Adapt a Kimi-backed provider to the standard provider interface for one request."""

    _provider: ChatProvider
    _generation_overrides: Mapping[str, Any]
    name: str = field(init=False)

    def __post_init__(self) -> None:
        self.name = self._provider.name

    @property
    def model_name(self) -> str:
        return self._provider.model_name

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return self._provider.thinking_effort

    async def generate(
        self,
        system_prompt: str,
        tools: Sequence[Tool],
        history: Sequence[Message],
    ) -> StreamedMessage:
        provider = cast(_GenerationOverrideProvider, self._provider)
        return await provider.generate(
            system_prompt,
            tools,
            history,
            generation_overrides=self._generation_overrides,
        )

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return type(self)(
            self._provider.with_thinking(effort),
            self._generation_overrides,
        )


@dataclass(slots=True)
class _TraceCallbackChatProvider:
    _provider: ChatProvider
    _on_trace_id: Callback[[str | None], None]
    name: str = field(init=False)

    def __post_init__(self) -> None:
        self.name = self._provider.name

    @property
    def model_name(self) -> str:
        return self._provider.model_name

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return self._provider.thinking_effort

    async def generate(
        self,
        system_prompt: str,
        tools: Sequence[Tool],
        history: Sequence[Message],
    ) -> StreamedMessage:
        await callback(self._on_trace_id, None)
        try:
            stream = await self._provider.generate(system_prompt, tools, history)
        except BaseException as error:
            if trace_id := getattr(error, "trace_id", None):
                await callback(self._on_trace_id, trace_id)
            raise
        await callback(self._on_trace_id, getattr(stream, "trace_id", None))
        return stream

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return type(self)(self._provider.with_thinking(effort), self._on_trace_id)


def find_kimi_provider(chat_provider: ChatProvider) -> Kimi | None:
    """Return the Kimi provider backing a supported provider wrapper."""
    from kosong.chat_provider.chaos import ChaosChatProvider
    from kosong.chat_provider.kimi import Kimi

    provider = chat_provider
    while isinstance(provider, ChaosChatProvider):
        provider = provider.wrapped_provider
    return provider if isinstance(provider, Kimi) else None


def with_kimi_generation_overrides(
    chat_provider: ChatProvider,
    generation_overrides: Mapping[str, Any] | None,
) -> ChatProvider:
    """Apply request-scoped generation overrides only to Kimi-backed providers."""
    if not generation_overrides or find_kimi_provider(chat_provider) is None:
        return chat_provider
    return _KimiRequestChatProvider(chat_provider, dict(generation_overrides))


def with_trace_callback(
    chat_provider: ChatProvider,
    on_trace_id: Callback[[str | None], None],
) -> ChatProvider:
    return _TraceCallbackChatProvider(chat_provider, on_trace_id)


def compute_max_completion_tokens(
    *,
    max_context_size: int,
    input_tokens: int,
    response_budget: int | None,
    fallback_budget: int = DEFAULT_UNKNOWN_CONTEXT_COMPLETION_TOKENS,
) -> int:
    """Compute completion cap from the hard cap and remaining context."""
    if max_context_size <= 0:
        return max(1, response_budget if response_budget is not None else fallback_budget)

    input_tokens = max(0, input_tokens)
    remaining = max(1, max_context_size - input_tokens)
    requested = response_budget if response_budget is not None else max_context_size
    return max(1, min(requested, remaining))


def estimate_request_tokens(
    system_prompt: str,
    tools: Sequence[Tool],
    history: Sequence[Message],
) -> int:
    """Estimate all token-bearing parts of a chat request."""
    return (
        _estimate_text_tokens(system_prompt)
        + sum(_estimate_tool_tokens(tool) for tool in tools)
        + sum(_estimate_message_tokens(message) for message in history)
    )


def estimate_openai_request_tokens(
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None = None,
) -> int:
    """Estimate tokens for an OpenAI-style chat request before it is sent.

    Uses the same character heuristic as ``estimate_request_tokens`` so the
    session loop can cap completion length before the provider counts tokens.
    """
    total = 0
    for message in messages:
        total += _estimate_text_tokens(str(message.get("role") or ""))
        name = message.get("name")
        if isinstance(name, str):
            total += _estimate_text_tokens(name)
        tool_call_id = message.get("tool_call_id")
        if isinstance(tool_call_id, str):
            total += _estimate_text_tokens(tool_call_id)
        total += _estimate_content_blob(message.get("content"))
        for tool_call in message.get("tool_calls") or ():
            if not isinstance(tool_call, Mapping):
                total += _estimate_text_tokens(str(tool_call))
                continue
            total += _estimate_text_tokens(str(tool_call.get("id") or ""))
            function = tool_call.get("function") or {}
            if isinstance(function, Mapping):
                total += _estimate_text_tokens(str(function.get("name") or ""))
                total += _estimate_text_tokens(str(function.get("arguments") or ""))
    if tools:
        total += _estimate_text_tokens(
            json.dumps(list(tools), ensure_ascii=False, separators=(",", ":"), default=str)
        )
    return total


def apply_request_completion_cap(
    request: dict[str, Any],
    *,
    context_limit: int,
    active_tokens: int = 0,
    response_budget: int | None = None,
    reserved_context_size: int = 50_000,
    safety_margin: int = DEFAULT_COMPLETION_TOKEN_SAFETY_MARGIN,
) -> int:
    """Shrink the completion budget so input plus output stays inside the window.

    ``active_tokens`` is the last provider usage. The payload estimate covers
    tool results appended after that usage. The larger of the two is the floor.
    """
    estimated = estimate_openai_request_tokens(
        request.get("messages") or [],
        request.get("tools"),
    )
    input_tokens = max(0, int(active_tokens or 0), estimated)
    if context_limit > 0:
        input_tokens += safety_margin
    configured = request.get("max_completion_tokens", request.get("max_tokens"))
    budget = configured if isinstance(configured, int) and configured > 0 else response_budget
    cap = compute_max_completion_tokens(
        max_context_size=context_limit,
        input_tokens=input_tokens,
        response_budget=budget if isinstance(budget, int) else None,
        fallback_budget=reserved_context_size,
    )
    model = str(request.get("model") or "").lower()
    # Newer OpenAI models reject ``max_tokens``; other providers reject the
    # completion-token field. Send exactly one.
    uses_completion_tokens = any(
        token in model for token in ("gpt-6", "gpt-5", "astra", "o1", "o3", "o4")
    )
    if uses_completion_tokens:
        request.pop("max_tokens", None)
        request["max_completion_tokens"] = cap
    else:
        request.pop("max_completion_tokens", None)
        request["max_tokens"] = cap
    return cap


def _estimate_content_blob(content: Any) -> int:
    if content is None:
        return 0
    if isinstance(content, str):
        return _estimate_text_tokens(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, str):
                total += _estimate_text_tokens(part)
            elif isinstance(part, Mapping):
                text = part.get("text") if isinstance(part.get("text"), str) else None
                if text is None and isinstance(part.get("content"), str):
                    text = part.get("content")
                if isinstance(text, str):
                    total += _estimate_text_tokens(text)
                else:
                    total += _estimate_text_tokens(
                        json.dumps(dict(part), ensure_ascii=False, default=str)
                    )
            else:
                total += _estimate_text_tokens(str(part))
        return total
    return _estimate_text_tokens(str(content))


def estimate_message_tokens(messages: Sequence[Message]) -> int:
    """Estimate token-bearing content for messages added outside the main context."""
    return sum(_estimate_message_tokens(message) for message in messages)


def _estimate_text_tokens(text: str) -> int:
    ascii_count = sum(char.isascii() for char in text)
    non_ascii_count = len(text) - ascii_count
    return (ascii_count + 3) // 4 + non_ascii_count


def _estimate_tool_tokens(tool: Tool) -> int:
    return (
        _estimate_text_tokens(tool.name)
        + _estimate_text_tokens(tool.description)
        + _estimate_text_tokens(
            json.dumps(tool.parameters, ensure_ascii=False, separators=(",", ":"))
        )
    )


def _estimate_message_tokens(message: Message) -> int:
    total = _estimate_text_tokens(message.role)
    if message.name:
        total += _estimate_text_tokens(message.name)
    if message.tool_call_id:
        total += _estimate_text_tokens(message.tool_call_id)

    for part in message.content:
        if isinstance(part, TextPart):
            total += _estimate_text_tokens(part.text)
        elif isinstance(part, ThinkPart):
            total += _estimate_text_tokens(part.think)
        elif isinstance(part, (ImageURLPart, AudioURLPart, VideoURLPart)):
            total += MEDIA_TOKEN_ESTIMATE
        else:
            total += _estimate_text_tokens(part.model_dump_json(exclude_none=True))

    for tool_call in message.tool_calls or ():
        total += _estimate_text_tokens(tool_call.id)
        total += _estimate_text_tokens(tool_call.function.name)
        total += _estimate_text_tokens(tool_call.function.arguments or "")
        if tool_call.extras:
            total += _estimate_text_tokens(
                json.dumps(tool_call.extras, ensure_ascii=False, separators=(",", ":"))
            )
    return total


def derive_model_capabilities(
    model: str | Any, declared: set[str] | None = None
) -> set[ModelCapability]:
    """Derive effective capabilities from model name/config and declared capabilities."""
    if hasattr(model, "model"):
        model_name = getattr(model, "model", "")
        model_caps = getattr(model, "capabilities", None)
        declared_set = (
            set(model_caps or ()) if declared is None else (declared | set(model_caps or ()))
        )
    else:
        model_name = str(model)
        declared_set = declared or set()

    capabilities: set[ModelCapability] = set(
        cast(ModelCapability, c) for c in declared_set if c in ALL_MODEL_CAPABILITIES
    )
    lowered = model_name.lower()
    if "thinking" in lowered or "reason" in lowered:
        capabilities.update(("thinking", "always_thinking"))
    elif model_name in {"kimi-for-coding", "kimi-code"}:
        capabilities.update(("thinking", "image_in", "video_in"))
    return capabilities


def model_display_name(model_name: str, display_name: str | None = None) -> str:
    """Derive display name: explicit display_name or fallback."""
    if display_name:
        return display_name
    return model_name


def is_always_thinking(model_name: str, capabilities: set[ModelCapability] | None = None) -> bool:
    caps = derive_model_capabilities(model_name, cast(set[str], capabilities))
    return "always_thinking" in caps


def supports_media(model_name: str, capabilities: set[ModelCapability] | None = None) -> bool:
    caps = derive_model_capabilities(model_name, cast(set[str], capabilities))
    return bool(caps & {"image_in", "video_in"})


def augment_provider_with_env_vars(provider: Any) -> Any:
    """Overlay environment variables onto a provider config copy."""
    prefix = f"CODERAI_PROVIDER_{provider.type.upper()}_"

    def _env(key: str) -> str | None:
        return os.getenv(prefix + key)

    updates: dict[str, Any] = {}
    if base_url := (
        _env("BASE_URL")
        or os.getenv("CODERAI_BASE_URL")
        or (os.getenv("KIMI_BASE_URL") if provider.type == "kimi" else None)
    ):
        updates["base_url"] = base_url
    if api_key := (
        _env("API_KEY")
        or os.getenv("CODERAI_API_KEY")
        or (os.getenv("KIMI_API_KEY") if provider.type == "kimi" else None)
    ):
        updates["api_key"] = SecretStr(api_key)
    if not updates:
        return provider
    return provider.model_copy(update=updates)


def _kimi_default_headers(provider: Any, oauth: Any | None = None) -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT}
    if oauth and hasattr(oauth, "common_headers"):
        headers.update(oauth.common_headers())
    custom_headers = getattr(provider, "custom_headers", None)
    if custom_headers:
        headers.update(custom_headers)
    return headers


def create_llm(
    provider: Any,
    model: Any,
    *,
    thinking: bool | None = None,
    session_id: str | None = None,
    oauth: Any | None = None,
) -> LLM | None:
    """Create an LLM instance backed by a kosong ChatProvider."""
    provider_type = getattr(provider, "type", "")
    base_url = getattr(provider, "base_url", None)
    model_name = getattr(model, "model", "")

    if provider_type not in {"_echo", "_scripted_echo"} and (not base_url or not model_name):
        logger.warning(
            "Cannot create LLM: missing base_url or model (provider_type={provider_type})",
            provider_type=provider_type,
        )
        return None

    api_key_obj = getattr(provider, "api_key", None)
    resolved_api_key = ""
    if oauth and getattr(provider, "oauth", None) and hasattr(oauth, "resolve_api_key"):
        resolved_api_key = oauth.resolve_api_key(api_key_obj, provider.oauth)
    elif api_key_obj is not None:
        resolved_api_key = (
            api_key_obj.get_secret_value()
            if hasattr(api_key_obj, "get_secret_value")
            else str(api_key_obj)
        )

    chat_provider: ChatProvider

    match provider_type:
        case "kimi":
            from kosong.chat_provider.kimi import Kimi

            chat_provider = Kimi(
                model=model_name,
                base_url=base_url,
                api_key=resolved_api_key,
                default_headers=_kimi_default_headers(provider, oauth),
            )

            gen_kwargs: Kimi.GenerationKwargs = {}
            if session_id:
                gen_kwargs["prompt_cache_key"] = session_id
            if temperature := os.getenv("CODERAI_MODEL_TEMPERATURE"):
                gen_kwargs["temperature"] = float(temperature)
            if top_p := os.getenv("CODERAI_MODEL_TOP_P"):
                gen_kwargs["top_p"] = float(top_p)
            for env_name in (
                "CODERAI_MODEL_MAX_COMPLETION_TOKENS",
                "CODERAI_MODEL_MAX_TOKENS",
            ):
                raw_max_completion_tokens = os.getenv(env_name)
                if not raw_max_completion_tokens:
                    continue
                try:
                    max_completion_tokens = int(raw_max_completion_tokens)
                except ValueError:
                    continue
                gen_kwargs["max_completion_tokens"] = (
                    max_completion_tokens if max_completion_tokens > 0 else None
                )
                break

            if gen_kwargs:
                chat_provider = chat_provider.with_generation_kwargs(**gen_kwargs)

        case "openai_legacy":
            from kosong.contrib.chat_provider.openai_legacy import OpenAILegacy

            reasoning_key = getattr(provider, "reasoning_key", None) or "reasoning_content"
            custom_headers = getattr(provider, "custom_headers", None)
            chat_provider = OpenAILegacy(
                model=model_name,
                base_url=base_url,
                api_key=resolved_api_key,
                reasoning_key=reasoning_key,
                default_headers=dict(custom_headers) if custom_headers else None,
            )

        case "openai_responses":
            from kosong.contrib.chat_provider.openai_responses import OpenAIResponses

            custom_headers = getattr(provider, "custom_headers", None)
            chat_provider = OpenAIResponses(
                model=model_name,
                base_url=base_url,
                api_key=resolved_api_key,
                default_headers=dict(custom_headers) if custom_headers else None,
            )

        case "anthropic":
            from kosong.contrib.chat_provider.anthropic import Anthropic

            custom_headers = getattr(provider, "custom_headers", None)
            chat_provider = Anthropic(
                model=model_name,
                base_url=base_url,
                api_key=resolved_api_key,
                default_max_tokens=50000,
                metadata={"user_id": session_id} if session_id else None,
                default_headers=dict(custom_headers) if custom_headers else None,
            )

        case "google_genai" | "gemini":
            from kosong.contrib.chat_provider.google_genai import GoogleGenAI

            custom_headers = getattr(provider, "custom_headers", None)
            chat_provider = GoogleGenAI(
                model=model_name,
                base_url=base_url,
                api_key=resolved_api_key,
                default_headers=dict(custom_headers) if custom_headers else None,
            )

        case "vertexai":
            from kosong.contrib.chat_provider.google_genai import GoogleGenAI

            env_vars = getattr(provider, "env", None)
            if env_vars:
                os.environ.update(env_vars)
            custom_headers = getattr(provider, "custom_headers", None)
            chat_provider = GoogleGenAI(
                model=model_name,
                base_url=base_url,
                api_key=resolved_api_key,
                vertexai=True,
                default_headers=dict(custom_headers) if custom_headers else None,
            )

        case "_echo":
            from kosong.chat_provider.echo import EchoChatProvider

            chat_provider = EchoChatProvider()

        case "_scripted_echo":
            from kosong.chat_provider.echo import ScriptedEchoChatProvider

            env_vars = getattr(provider, "env", None)
            if env_vars:
                os.environ.update(env_vars)
            scripts = _load_scripted_echo_scripts()
            trace_value = os.getenv("CODERAI_SCRIPTED_ECHO_TRACE", "")
            trace = trace_value.strip().lower() in {"1", "true", "yes", "on"}
            chat_provider = ScriptedEchoChatProvider(scripts, trace=trace)

        case "_chaos":
            from kosong.chat_provider.chaos import ChaosChatProvider, ChaosConfig
            from kosong.chat_provider.kimi import Kimi

            chat_provider = ChaosChatProvider(
                provider=Kimi(
                    model=model_name,
                    base_url=base_url,
                    api_key=resolved_api_key,
                    default_headers=_kimi_default_headers(provider, oauth),
                ),
                chaos_config=ChaosConfig(
                    error_probability=0.8,
                    error_types=[429, 500, 503],
                ),
            )

        case _:
            # Fallback default to OpenAILegacy for other OpenAI-compatible endpoints
            from kosong.contrib.chat_provider.openai_legacy import OpenAILegacy

            custom_headers = getattr(provider, "custom_headers", None)
            chat_provider = OpenAILegacy(
                model=model_name,
                base_url=base_url,
                api_key=resolved_api_key,
                default_headers=dict(custom_headers) if custom_headers else None,
            )

    capabilities = derive_model_capabilities(model)

    thinking_on = "always_thinking" in capabilities or (
        thinking is True and "thinking" in capabilities
    )
    if thinking_on:
        chat_provider = chat_provider.with_thinking("high")
    elif thinking is False:
        chat_provider = chat_provider.with_thinking("off")

    if thinking_on and provider_type == "kimi":
        from kosong.chat_provider.kimi import Kimi

        if isinstance(chat_provider, Kimi) and (
            thinking_keep := os.getenv("CODERAI_MODEL_THINKING_KEEP")
        ):
            chat_provider = chat_provider.with_extra_body({"thinking": {"keep": thinking_keep}})

    max_ctx = getattr(model, "max_context_size", 128000)
    return LLM(
        chat_provider=chat_provider,
        max_context_size=max_ctx,
        capabilities=capabilities,
        model_config=model,
        provider_config=provider,
    )


def clone_llm_with_model_alias(
    llm: LLM | None,
    config: Any,
    model_alias: str | None,
    *,
    session_id: str,
    oauth: Any | None = None,
) -> LLM | None:
    if model_alias is None:
        return llm
    models = getattr(config, "models", {})
    if model_alias not in models:
        raise KeyError(f"Unknown model alias: {model_alias}")
    model = models[model_alias]
    providers = getattr(config, "providers", {})
    provider = providers[model.provider]
    thinking: bool | None = None
    if llm is not None:
        effort = getattr(llm.chat_provider, "thinking_effort", None)
        if effort is not None:
            thinking = effort != "off"
    return create_llm(
        provider,
        model,
        thinking=thinking,
        session_id=session_id,
        oauth=oauth,
    )


def _load_scripted_echo_scripts() -> list[str]:
    script_path = os.getenv("CODERAI_SCRIPTED_ECHO_SCRIPTS")
    if not script_path or not Path(script_path).is_file():
        raise ValueError("CODERAI_SCRIPTED_ECHO_SCRIPTS is required for _scripted_echo.")
    path = Path(script_path).expanduser()
    if not path.exists():
        raise ValueError(f"Scripted echo file not found: {path}")
    text = path.read_text(encoding="utf-8")
    try:
        data: object = json.loads(text)
    except json.JSONDecodeError:
        scripts = [chunk.strip() for chunk in text.split("\n---\n") if chunk.strip()]
        if scripts:
            return scripts
        raise ValueError(
            "Scripted echo file must be a JSON array of strings or a text file split by '\\n---\\n'."
        ) from None
    if isinstance(data, list):
        data_list = cast(list[object], data)
        if all(isinstance(item, str) for item in data_list):
            return cast(list[str], data_list)
    raise ValueError("Scripted echo JSON must be an array of strings.")


# ---------------------------------------------------------------------------
# CoderAI Client Pool and Routing Infrastructure (Preserved from original)
# ---------------------------------------------------------------------------

PROVIDER_BASE_URLS: dict[str, str] = {
    "deepseek": "https://api.deepseek.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
    "together": "https://api.together.xyz/v1",
    "fireworks": "https://api.fireworks.ai/inference/v1",
    "openai": "https://api.openai.com/v1",
    "kimi": "https://api.kimi.com/coding/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "ollama": "http://localhost:11434/v1",
    "vllm": "http://localhost:8000/v1",
    "lmstudio": "http://localhost:1234/v1",
}


DEFAULT_BASE_URL = "https://api.openai.com/v1"


def resolve_model_provider_routing(
    model: str,
    explicit_base_url: str | None = None,
    explicit_api_key: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[str | None, str | None]:
    """Resolve the appropriate baseURL and API key for the selected model."""
    env = env or {}
    m = model.strip().lower()

    # 0. Jev System-One is NAR triage/gating only — never a chat/tool model.
    # Return its key with a jev:// marker so callers can fail with a clear message.
    if m in {"jev-system-one"} or m.startswith("jev-") or m.startswith("typesafe"):
        jev_key = (
            explicit_api_key
            or env.get("TYPESAFE_API_KEY")
            or os.getenv("TYPESAFE_API_KEY")
            or env.get("JEV_API_KEY")
            or os.getenv("JEV_API_KEY")
        )
        return "jev://system-one", jev_key

    # 1. If user explicitly provided a non-default custom baseURL in settings/env, respect it.
    if explicit_base_url and explicit_base_url != DEFAULT_BASE_URL:
        api_key = (
            explicit_api_key
            or env.get("API_KEY")
            or os.getenv("CODERAI_API_KEY")
            or os.getenv("OPENAI_API_KEY")
        )
        return explicit_base_url, api_key

    # 2. DeepSeek models
    if m.startswith("deepseek-") or m.startswith("deepseek/"):
        base_url = (
            env.get("DEEPSEEK_BASE_URL")
            or os.getenv("DEEPSEEK_BASE_URL")
            or PROVIDER_BASE_URLS.get("deepseek", "https://api.deepseek.com")
        )
        api_key = (
            env.get("DEEPSEEK_API_KEY")
            or os.getenv("DEEPSEEK_API_KEY")
            or explicit_api_key
            or os.getenv("OPENAI_API_KEY")
        )
        return base_url, api_key

    # 3. Google Gemini models
    if m.startswith("gemini-") or m.startswith("google/"):
        base_url = (
            env.get("GEMINI_BASE_URL")
            or os.getenv("GEMINI_BASE_URL")
            or PROVIDER_BASE_URLS.get(
                "gemini", "https://generativelanguage.googleapis.com/v1beta/openai/"
            )
        )
        api_key = (
            env.get("GEMINI_API_KEY")
            or os.getenv("GEMINI_API_KEY")
            or env.get("GOOGLE_API_KEY")
            or os.getenv("GOOGLE_API_KEY")
            or explicit_api_key
            or os.getenv("OPENAI_API_KEY")
        )
        return base_url, api_key

    # 4. Anthropic Claude models
    if m.startswith("claude-") or m.startswith("anthropic/"):
        anthropic_url = env.get("ANTHROPIC_BASE_URL") or os.getenv("ANTHROPIC_BASE_URL")
        openrouter_key = env.get("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_API_KEY")
        if anthropic_url:
            base_url = anthropic_url
            api_key = (
                env.get("ANTHROPIC_API_KEY")
                or os.getenv("ANTHROPIC_API_KEY")
                or explicit_api_key
                or os.getenv("OPENAI_API_KEY")
            )
        elif openrouter_key:
            base_url = (
                env.get("OPENROUTER_BASE_URL")
                or os.getenv("OPENROUTER_BASE_URL")
                or PROVIDER_BASE_URLS.get("openrouter", "https://openrouter.ai/api/v1")
            )
            api_key = openrouter_key
        else:
            base_url = explicit_base_url or os.getenv("OPENAI_BASE_URL") or DEFAULT_BASE_URL
            api_key = (
                env.get("ANTHROPIC_API_KEY")
                or os.getenv("ANTHROPIC_API_KEY")
                or explicit_api_key
                or os.getenv("OPENAI_API_KEY")
            )
        return base_url, api_key

    # 5. Kimi / Moonshot models
    if m.startswith("kimi") or "kimi" in m or m.startswith("moonshot"):
        base_url = (
            env.get("KIMI_BASE_URL")
            or os.getenv("KIMI_BASE_URL")
            or env.get("MOONSHOT_BASE_URL")
            or os.getenv("MOONSHOT_BASE_URL")
            or PROVIDER_BASE_URLS.get("kimi", "https://api.kimi.com/coding/v1")
        )
        oauth_token = None
        try:
            from coderai.auth.oauth import KIMI_CODE_OAUTH_KEY, load_token

            tok = load_token(KIMI_CODE_OAUTH_KEY)
            if tok and tok.access_token:
                oauth_token = tok.access_token
        except Exception:
            pass

        # Don't let a generic OpenAI project key from settings.json hijack Kimi requests
        clean_explicit = explicit_api_key
        if clean_explicit and clean_explicit.startswith("sk-proj-"):
            clean_explicit = None

        api_key = (
            env.get("KIMI_API_KEY")
            or os.getenv("KIMI_API_KEY")
            or env.get("MOONSHOT_API_KEY")
            or os.getenv("MOONSHOT_API_KEY")
            or oauth_token
            or clean_explicit
            or explicit_api_key
            or os.getenv("OPENAI_API_KEY")
        )
        return base_url, api_key

    # 6. OpenRouter prefix models
    if m.startswith("openrouter/") or m.startswith("openrouter-"):
        base_url = (
            env.get("OPENROUTER_BASE_URL")
            or os.getenv("OPENROUTER_BASE_URL")
            or PROVIDER_BASE_URLS.get("openrouter", "https://openrouter.ai/api/v1")
        )
        api_key = (
            env.get("OPENROUTER_API_KEY")
            or os.getenv("OPENROUTER_API_KEY")
            or explicit_api_key
            or os.getenv("OPENAI_API_KEY")
        )
        return base_url, api_key

    # 7. Default OpenAI / Fallback
    base_url = explicit_base_url or os.getenv("OPENAI_BASE_URL") or DEFAULT_BASE_URL
    api_key = (
        explicit_api_key
        or env.get("API_KEY")
        or os.getenv("CODERAI_API_KEY")
        or os.getenv("OPENAI_API_KEY")
    )
    return base_url, api_key


class _EchoToolCallFunction:
    def __init__(self, name: str | None = None, arguments: str | None = None) -> None:
        self.name = name
        self.arguments = arguments


class _EchoToolCallDelta:
    def __init__(
        self,
        index: int = 0,
        id: str | None = None,
        function: _EchoToolCallFunction | None = None,
        type: str = "function",
    ) -> None:
        self.index = index
        self.id = id
        self.function = function
        self.type = type


class _EchoDelta:
    def __init__(
        self,
        content: str | None = None,
        tool_calls: list[Any] | None = None,
        reasoning_content: str | None = None,
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = reasoning_content


class _EchoChunkChoice:
    def __init__(
        self,
        delta: _EchoDelta,
        finish_reason: str | None = None,
        index: int = 0,
    ) -> None:
        self.delta = delta
        self.finish_reason = finish_reason
        self.index = index


class _EchoChunkUsage:
    def __init__(
        self,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
    ) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


class _EchoChunk:
    def __init__(
        self,
        choices: list[_EchoChunkChoice],
        usage: Any = None,
    ) -> None:
        self.choices = choices
        self.usage = usage


class _EchoCompletions:
    def __init__(self, chat_provider: Any) -> None:
        self.chat_provider = chat_provider

    def create(self, **kwargs: Any) -> Any:
        if self.chat_provider is None:
            return [
                _EchoChunk([_EchoChunkChoice(_EchoDelta(content="ok"))]),
                _EchoChunk([_EchoChunkChoice(_EchoDelta(), finish_reason="stop")]),
            ]

        if not hasattr(self.chat_provider, "_scripts"):
            messages = kwargs.get("messages") or []
            last_text = ""
            if messages:
                last = messages[-1]
                last_text = (
                    last.get("content", "")
                    if isinstance(last, dict)
                    else str(getattr(last, "content", ""))
                )
            return [
                _EchoChunk([_EchoChunkChoice(_EchoDelta(content=f"echo: {last_text}"))]),
                _EchoChunk([_EchoChunkChoice(_EchoDelta(), finish_reason="stop")]),
            ]

        if not self.chat_provider._scripts:
            from kosong.chat_provider import ChatProviderError

            turn = getattr(self.chat_provider, "_turn", 0) + 1
            raise ChatProviderError(f"ScriptedEchoChatProvider exhausted at turn {turn}.")

        script_text = self.chat_provider._scripts.popleft()
        if getattr(self.chat_provider, "_trace", False):
            import json

            turn = getattr(self.chat_provider, "_turn", 0) + 1
            print(f"SCRIPTED_ECHO TURN {turn}: {json.dumps(script_text)}")
        self.chat_provider._turn = getattr(self.chat_provider, "_turn", 0) + 1

        from kosong.chat_provider.echo.dsl import parse_echo_script

        parts, message_id, usage = parse_echo_script(script_text)

        chunks: list[_EchoChunk] = []
        for p in parts:
            p_type = getattr(p, "type", "")
            if p_type == "text":
                chunks.append(
                    _EchoChunk([_EchoChunkChoice(_EchoDelta(content=getattr(p, "text", "")))])
                )
            elif p_type == "think":
                chunks.append(
                    _EchoChunk(
                        [_EchoChunkChoice(_EchoDelta(reasoning_content=getattr(p, "think", "")))]
                    )
                )
            elif hasattr(p, "function") or p_type in ("tool_call", "function"):
                fn_name = getattr(p.function, "name", "") if hasattr(p, "function") else ""
                fn_args = getattr(p.function, "arguments", "") if hasattr(p, "function") else ""
                tc_delta = _EchoToolCallDelta(
                    index=getattr(p, "index", 0),
                    id=getattr(p, "id", None) or f"call_{len(chunks)}",
                    function=_EchoToolCallFunction(name=fn_name, arguments=fn_args),
                )
                chunks.append(_EchoChunk([_EchoChunkChoice(_EchoDelta(tool_calls=[tc_delta]))]))
            elif p_type == "tool_call_part":
                tc_delta = _EchoToolCallDelta(
                    index=getattr(p, "index", 0),
                    id=getattr(p, "id", None),
                    function=_EchoToolCallFunction(
                        name=getattr(p, "function_name", None),
                        arguments=getattr(p, "arguments_part", ""),
                    ),
                )
                chunks.append(_EchoChunk([_EchoChunkChoice(_EchoDelta(tool_calls=[tc_delta]))]))

        has_tool_call = any(
            hasattr(p, "function")
            or getattr(p, "type", "") in ("tool_call", "tool_call_part", "function")
            for p in parts
        )
        finish = "tool_calls" if has_tool_call else "stop"
        u_obj = None
        if usage:
            u_obj = _EchoChunkUsage(
                prompt_tokens=getattr(usage, "input_tokens", 0) or 0,
                completion_tokens=getattr(usage, "output_tokens", 0) or 0,
                total_tokens=(getattr(usage, "input_tokens", 0) or 0)
                + (getattr(usage, "output_tokens", 0) or 0),
            )
        chunks.append(
            _EchoChunk([_EchoChunkChoice(_EchoDelta(), finish_reason=finish)], usage=u_obj)
        )
        return chunks


class _EchoChat:
    def __init__(self, chat_provider: Any) -> None:
        self.completions = _EchoCompletions(chat_provider)


class _EchoClientAdapter:
    def __init__(self, chat_provider: Any) -> None:
        self.chat = _EchoChat(chat_provider)


_client_pool: dict[str, Any] = {}


def create_openai_client(
    project_root: str = ".", model_override: str | None = None
) -> dict[str, Any]:
    global _client_pool
    from coderai.config import resolve_current_settings
    from coderai.utils.common.model_capabilities import defaults_to_thinking_mode

    settings = resolve_current_settings(project_root)
    active_model = model_override or settings["model"]
    configured_key = settings.get("apiKey")
    configured_base_url = settings.get("baseURL")
    env = settings.get("env", {})

    provider_type = "openai_legacy"
    declared_caps: set[str] | None = None
    display_name: str | None = None
    max_context_size: int | None = None
    custom_headers: dict[str, str] | None = None
    reasoning_key = "reasoning_content"
    oauth_key: str | None = None
    tmodel: Any = None
    tprovider: Any = None
    try:
        from coderai.config import load_typed_config

        typed = load_typed_config()
        alias = None
        for key, m in typed.models.items():
            if key == active_model or m.model == active_model:
                alias = key
                break
        tmodel = typed.models.get(alias) if alias else None
        tprovider = typed.providers.get(tmodel.provider) if tmodel else None
        if tmodel is not None and tprovider is not None:
            provider_type = tprovider.type
            declared_caps = set(tmodel.capabilities or ())
            display_name = tmodel.display_name
            max_context_size = tmodel.max_context_size
            custom_headers = dict(tprovider.custom_headers) if tprovider.custom_headers else None
            if tprovider.reasoning_key:
                reasoning_key = tprovider.reasoning_key
            if tprovider.oauth is not None:
                oauth_key = tprovider.oauth.key
            if not configured_key:
                configured_key = tprovider.api_key.get_secret_value()
            if configured_base_url in (None, DEFAULT_BASE_URL):
                configured_base_url = tprovider.base_url
    except Exception:
        pass

    current = {"base_url": configured_base_url or "", "api_key": configured_key or ""}
    try:
        prefix = f"CODERAI_PROVIDER_{provider_type.upper()}_"
        b_url = os.getenv(prefix + "BASE_URL")
        a_key = os.getenv(prefix + "API_KEY")
        if b_url:
            current["base_url"] = b_url
        if a_key:
            current["api_key"] = a_key
    except Exception:
        pass
    if current.get("base_url"):
        configured_base_url = current["base_url"]
    if current.get("api_key"):
        configured_key = current["api_key"]

    if oauth_key:
        try:
            from coderai.auth.oauth import OAuthManager

            resolved_oauth = OAuthManager([oauth_key]).resolve_api_key(
                configured_key or "", oauth_key
            )
            if resolved_oauth:
                configured_key = resolved_oauth
        except Exception:
            pass

    base_url, api_key = resolve_model_provider_routing(
        model=active_model,
        explicit_base_url=configured_base_url,
        explicit_api_key=configured_key,
        env=env,
    )

    if settings.get("thinkingEnabled") is not None and model_override is None:
        thinking_enabled = bool(settings.get("thinkingEnabled"))
    else:
        thinking_enabled = defaults_to_thinking_mode(active_model)

    capabilities = derive_model_capabilities(active_model, declared_caps)
    context_window = max_context_size or settings.get("contextWindow") or 256 * 1024

    wire_model = active_model
    if tmodel is not None and tmodel.model:
        wire_model = tmodel.model
    elif "/" in active_model and not active_model.startswith("openrouter/"):
        wire_model = active_model.split("/", 1)[-1]

    def base() -> dict[str, Any]:
        return {
            "client": None,
            "model": wire_model,
            "displayModel": active_model,
            "displayName": model_display_name(active_model, display_name),
            "capabilities": sorted(capabilities),
            "providerType": provider_type,
            "contextWindow": context_window,
            "baseURL": base_url,
            "temperature": settings.get("temperature"),
            "thinkingEnabled": thinking_enabled,
            "reasoningEffort": settings.get("reasoningEffort", "max"),
            "reasoningKey": reasoning_key,
            "customHeaders": custom_headers,
            "oauthKey": oauth_key,
            "debugLogEnabled": settings.get("debugLogEnabled", False),
            "telemetryEnabled": settings.get("telemetryEnabled", False),
            "notify": settings.get("notify"),
            "webSearchTool": settings.get("webSearchTool"),
            "env": env,
        }

    if provider_type in {"_scripted_echo", "_echo"}:
        cache_key = f"_echo::{provider_type}::{active_model}"
        if cache_key in _client_pool:
            result = base()
            result["client"] = _client_pool[cache_key]
            return result
        try:
            from coderai.config import LLMProvider as _LLMP, LLMModel as _LLMM
            from pydantic import SecretStr as _Sec

            _sec_key = _Sec(api_key or "none")
            _prov = tprovider or _LLMP(
                type=provider_type, base_url=base_url or "", api_key=_sec_key
            )
            _mod = tmodel or _LLMM(
                provider=_prov.type if hasattr(_prov, "type") else "scripted_provider",
                model=active_model,
                max_context_size=context_window,
            )
            _llm_inst = create_llm(_prov, _mod)
            client_adapter = _EchoClientAdapter(_llm_inst.chat_provider)
            _client_pool[cache_key] = client_adapter
            result = base()
            result["client"] = client_adapter
            return result
        except Exception:
            raise

    if not api_key or base_url == "jev://system-one":
        return base()

    cache_key = f"{api_key}::{base_url}"
    if cache_key in _client_pool:
        result = base()
        result["client"] = _client_pool[cache_key]
        return result

    try:
        from openai import OpenAI

        client_instance = OpenAI(
            api_key=api_key,
            base_url=base_url or None,
            default_headers=custom_headers,
        )
        _client_pool[cache_key] = client_instance
    except Exception:
        client_instance = None

    result = base()
    result["client"] = client_instance
    return result


def clear_client_pool() -> None:
    """Clear all cached OpenAI client instances."""
    global _client_pool
    _client_pool.clear()


async def ensure_oauth_fresh(*, force: bool = False) -> None:
    pass


def probe_provider_connectivity(
    model: str,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: float = 10.0,
) -> tuple[bool, str]:
    """Probe API connection to a provider with given model, endpoint, and key."""
    # Jev System-One is NAR (TypeSafe SDK), not OpenAI-compatible — probe it directly.
    try:
        from coderai.utils.common.model_capabilities import is_jev_model

        if is_jev_model(model or ""):
            from coderai.jev.client import jev_status

            status = jev_status() if not api_key else None
            if status is not None and not status.get("configured"):
                return (
                    False,
                    "Jev System-One not configured: set TYPESAFE_API_KEY (or JEV_API_KEY).",
                )
            # Lightweight live probe when reachable; never raise.
            try:
                from coderai.triage.engine import get_triage_engine

                engine = get_triage_engine(api_key=api_key)
                if not engine.is_available:
                    if status is not None:
                        return (
                            False,
                            "Jev client unavailable: install typesafe_sdk and set TYPESAFE_API_KEY.",
                        )
                    return (
                        False,
                        "No API key provided for Jev System-One (TYPESAFE_API_KEY).",
                    )
                probe = engine.screen_diff_hunk("probe.py", "x = 1\n")
                if probe.reason.startswith("Triage error fallback"):
                    return False, f"Jev probe failed: {probe.reason[:160]}"
                return True, "Successfully connected! Jev System-One triage responded."
            except Exception as exc:
                return False, f"Jev probe error ({type(exc).__name__}): {str(exc)[:120]}"
    except Exception:
        pass
    wire_model = model
    try:
        from coderai.config import load_typed_config

        typed = load_typed_config()
        alias = None
        for key, m in typed.models.items():
            if key == model or m.model == model:
                alias = key
                break
        if alias:
            tmodel = typed.models[alias]
            tprovider = typed.providers.get(tmodel.provider)
            if tmodel.model:
                wire_model = tmodel.model
            if tprovider:
                if not base_url or base_url == DEFAULT_BASE_URL:
                    base_url = tprovider.base_url
                if not api_key:
                    api_key = tprovider.api_key.get_secret_value()
                if tprovider.oauth is not None:
                    try:
                        from coderai.auth.oauth import OAuthManager

                        resolved_oauth = OAuthManager([tprovider.oauth.key]).resolve_api_key(
                            api_key or "", tprovider.oauth.key
                        )
                        if resolved_oauth:
                            api_key = resolved_oauth
                    except Exception:
                        pass
        elif "/" in model and not model.startswith("openrouter/"):
            wire_model = model.split("/", 1)[-1]
    except Exception:
        pass

    resolved_url, resolved_key = resolve_model_provider_routing(
        model=model,
        explicit_base_url=base_url,
        explicit_api_key=api_key,
    )

    if not resolved_key:
        return (
            False,
            f"No API key provided or resolved for model '{model}' (endpoint: {resolved_url}).",
        )

    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=resolved_key,
            base_url=resolved_url or None,
            timeout=timeout,
            max_retries=1,
        )

        resp = None
        probe_errors: list[str] = []

        try:
            resp = client.chat.completions.create(
                model=wire_model,
                messages=[{"role": "user", "content": "ping"}],
                max_completion_tokens=5,
            )
        except Exception as e_comp:
            probe_errors.append(str(e_comp))
            if any(
                k in str(e_comp) for k in ("max_completion_tokens", "Unsupported parameter", "400")
            ):
                try:
                    resp = client.chat.completions.create(
                        model=wire_model,
                        messages=[{"role": "user", "content": "ping"}],
                        max_tokens=5,
                    )
                except Exception as e_max:
                    probe_errors.append(str(e_max))
                    try:
                        resp = client.chat.completions.create(
                            model=wire_model,
                            messages=[{"role": "user", "content": "ping"}],
                        )
                    except Exception as e_plain:
                        raise e_plain
            else:
                raise e_comp

        model_name = getattr(resp, "model", wire_model) if resp else wire_model
        return True, f"Successfully connected! Model response received from '{model_name}'."
    except Exception as e:
        err_str = str(e)
        if "AuthenticationError" in type(e).__name__ or "401" in err_str:
            return False, "Authentication Failed: Invalid API key (401 Unauthorized)."
        if "PermissionDeniedError" in type(e).__name__ or "403" in err_str:
            if "limit" in err_str.lower() or "quota" in err_str.lower():
                return (
                    False,
                    "Quota Exceeded (403): You've reached your monthly usage limit for this account or model.",
                )
            return False, f"Permission Denied (403): Access denied to model '{wire_model}'."
        if "NotFoundError" in type(e).__name__ or "404" in err_str:
            return (
                False,
                f"Model Not Found (404): Endpoint '{resolved_url}' does not recognize '{wire_model}'.",
            )
        if "RateLimitError" in type(e).__name__ or "429" in err_str:
            return False, "Rate Limit Exceeded (429): Quota or rate limit reached on provider."
        if "APIConnectionError" in type(e).__name__ or "ConnectError" in err_str:
            return False, f"Connection Failed: Could not reach endpoint '{resolved_url}'."
        return False, f"Connection test error ({type(e).__name__}): {err_str[:120]}"


check_provider_connectivity = probe_provider_connectivity
