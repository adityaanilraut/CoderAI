"""LLM Provider Factory."""

from typing import Any
from .openai import OpenAIProvider
from .anthropic import AnthropicProvider
from .lmstudio import LMStudioProvider
from .ollama import OllamaProvider
from .groq import GroqProvider
from .deepseek import DeepSeekProvider

def create_provider(model: str, config: Any) -> Any:
    """Create LLM provider based on model."""
    if model == "ollama":
        return OllamaProvider(
            model=config.ollama_model,
            endpoint=config.ollama_endpoint,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
    elif model == "lmstudio":
        return LMStudioProvider(
            model=config.lmstudio_model,
            endpoint=config.lmstudio_endpoint,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
    elif model.startswith("claude"):
        return AnthropicProvider(
            model=model,
            api_key=config.anthropic_api_key,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            reasoning_effort=config.reasoning_effort,
        )
    elif model in GroqProvider.SUPPORTED_MODELS or model.startswith("groq/"):
        actual_model = model.replace("groq/", "") if model.startswith("groq/") else model
        return GroqProvider(
            model=actual_model,
            api_key=config.groq_api_key,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
    elif model in DeepSeekProvider.SUPPORTED_MODELS or model.startswith("deepseek/"):
        actual_model = model.replace("deepseek/", "") if model.startswith("deepseek/") else model
        return DeepSeekProvider(
            model=actual_model,
            api_key=config.deepseek_api_key,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
    else:
        return OpenAIProvider(
            model=model,
            api_key=config.openai_api_key,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            reasoning_effort=config.reasoning_effort,
        )
