"""LLM Provider Factory."""

from typing import Any
from .openai import OpenAIProvider
from .anthropic import AnthropicProvider, MODEL_ALIASES as ANTHROPIC_MODEL_ALIASES
from .lmstudio import LMStudioProvider
from .ollama import OllamaProvider
from .groq import GroqProvider
from .deepseek import DeepSeekProvider


def create_provider(model: str, config: Any) -> Any:
    """Create LLM provider based on model.

    Raises ``ValueError`` when ``model`` is not recognized by any provider.
    We intentionally do *not* fall back to OpenAI: a typo in
    ``CODERAI_DEFAULT_MODEL`` should fail loudly rather than silently
    routing traffic (and keys) to the wrong backend.
    """
    if model == "ollama":
        return OllamaProvider(
            model=config.ollama_model,
            endpoint=config.ollama_endpoint,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
    if model == "lmstudio":
        return LMStudioProvider(
            model=config.lmstudio_model,
            endpoint=config.lmstudio_endpoint,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
    if (
        model.startswith("claude")
        or model in ANTHROPIC_MODEL_ALIASES
        or model in ("sonnet", "opus", "haiku")
    ):
        return AnthropicProvider(
            model=model,
            api_key=config.anthropic_api_key,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            reasoning_effort=config.reasoning_effort,
        )
    if model in GroqProvider.SUPPORTED_MODELS or model.startswith("groq/"):
        actual_model = model.replace("groq/", "") if model.startswith("groq/") else model
        return GroqProvider(
            model=actual_model,
            api_key=config.groq_api_key,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
    if model in DeepSeekProvider.SUPPORTED_MODELS or model.startswith("deepseek/"):
        actual_model = model.replace("deepseek/", "") if model.startswith("deepseek/") else model
        return DeepSeekProvider(
            model=actual_model,
            api_key=config.deepseek_api_key,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
    if model in OpenAIProvider.SUPPORTED_MODELS or model.startswith(("gpt-", "o1", "o3", "openai/")):
        actual_model = model.replace("openai/", "") if model.startswith("openai/") else model
        return OpenAIProvider(
            model=actual_model,
            api_key=config.openai_api_key,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            reasoning_effort=config.reasoning_effort,
        )

    raise ValueError(
        f"Unknown model: {model!r}. "
        "Use one of the listed aliases (sonnet/opus/haiku), a full provider "
        "model ID (e.g. claude-opus-4-7, gpt-5.4, deepseek-v3), or a prefixed "
        "form like groq/<name>, deepseek/<name>, openai/<name>. "
        "Run `coderAI models` to see the full list."
    )
