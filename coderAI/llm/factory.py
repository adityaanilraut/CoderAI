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
    model_lower = model.lower()
    if model_lower == "ollama" or model_lower.startswith("ollama/"):
        actual_model = model.split("/", 1)[1] if model.startswith("ollama/") else config.ollama_model
        return OllamaProvider(
            model=actual_model,
            endpoint=config.ollama_endpoint,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
    if model_lower == "lmstudio" or model_lower.startswith("lmstudio/"):
        actual_model = model.split("/", 1)[1] if model.startswith("lmstudio/") else config.lmstudio_model
        return LMStudioProvider(
            model=actual_model,
            endpoint=config.lmstudio_endpoint,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
    if (
        model_lower.startswith("claude")
        or model_lower in ANTHROPIC_MODEL_ALIASES
        or model_lower in ("sonnet", "opus", "haiku")
    ):
        return AnthropicProvider(
            model=model,
            api_key=config.anthropic_api_key,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            reasoning_effort=config.reasoning_effort,
        )
    if model_lower in GroqProvider.SUPPORTED_MODELS or model_lower.startswith("groq/"):
        actual_model = model.split("groq/", 1)[1] if model_lower.startswith("groq/") else model
        return GroqProvider(
            model=actual_model,
            api_key=config.groq_api_key,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
    if model_lower in (k.lower() for k in DeepSeekProvider.SUPPORTED_MODELS) or model_lower.startswith("deepseek/"):
        actual_model = model.split("deepseek/", 1)[1] if model_lower.startswith("deepseek/") else model
        return DeepSeekProvider(
            model=actual_model,
            api_key=config.deepseek_api_key,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
    if model_lower in OpenAIProvider.SUPPORTED_MODELS or model_lower.startswith(("o1-", "o3", "gpt-", "openai/")):
        actual_model = model.split("openai/", 1)[1] if model_lower.startswith("openai/") else model
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
