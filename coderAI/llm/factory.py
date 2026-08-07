"""LLM Provider Factory — thin seam over unified registry."""

from typing import Any

from coderAI.llm.registry import (
    resolve_alias,
    provider_for_model as _provider_for_model,
    get_models_by_provider as _registry_models_by_provider,
)

# Re-export alias map for compat imports (tests import from anthropic)
from coderAI.llm.anthropic import MODEL_ALIASES as ANTHROPIC_MODEL_ALIASES  # noqa: F401


def provider_id_for_model(model: str) -> str:
    """Return provider id for *model* (alias-aware, registry-driven)."""
    low = (model or "").strip().lower()
    if not low or ("/" in low and not low.split("/", 1)[1]):
        raise ValueError(f"Unknown model: {model!r}. Run `coderAI models` to see valid models.")
    # direct registry lookup first
    prov = _provider_for_model(model)
    if prov:
        return prov
    # fallback prefix checks for prefixed forms not in registry
    if low.startswith("groq/"):
        return "groq"
    if low.startswith("deepseek/"):
        return "deepseek"
    if low.startswith(("gemini-", "gemini/")):
        return "gemini"
    if low.startswith(("meta/", "muse")):
        return "meta"
    if low.startswith(("gpt-", "openai/")):
        return "openai"
    if low.startswith(("claude", "anthropic/")) or low in ("fable", "sonnet", "opus", "haiku"):
        return "anthropic"
    if low in ("ollama", "lmstudio") or low.startswith(("ollama/", "lmstudio/")):
        return low.split("/")[0]
    raise ValueError(f"Unknown model: {model!r}. Run `coderAI models` to see valid models.")


def is_valid_model_name(model: str) -> bool:
    try:
        provider_id_for_model(model)
    except ValueError:
        return False
    return True


def resolve_model_alias(name: str) -> str:
    if not isinstance(name, str):
        return name
    return resolve_alias(name)


def get_all_model_ids() -> set[str]:
    from coderAI.llm.registry import all_canonical_ids, _ALIAS_TO_ID

    return all_canonical_ids() | set(_ALIAS_TO_ID.keys())


def get_models_by_provider() -> list[tuple[str, list[str], str]]:
    """Delegates to registry — no hard-coded lists."""
    return _registry_models_by_provider()


def create_provider(model: str, config: Any) -> Any:
    """Create provider (registry-aware, no hard-coded model tables)."""
    # Lazy imports to avoid circular at module load
    from coderAI.llm.anthropic import AnthropicProvider
    from coderAI.llm.deepseek import DeepSeekProvider
    from coderAI.llm.gemini import GeminiProvider
    from coderAI.llm.groq import GroqProvider
    from coderAI.llm.lmstudio import LMStudioProvider
    from coderAI.llm.meta import MetaProvider
    from coderAI.llm.ollama import OllamaProvider
    from coderAI.llm.openai import OpenAIProvider

    model = model.strip()
    low = model.lower()

    prov = provider_id_for_model(model)

    if prov == "ollama":
        actual = model.split("/", 1)[1] if "/" in model else config.ollama_model
        return OllamaProvider(
            model=actual,
            endpoint=config.ollama_endpoint,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
    if prov == "lmstudio":
        actual = model.split("/", 1)[1] if "/" in model else config.lmstudio_model
        return LMStudioProvider(
            model=actual,
            endpoint=config.lmstudio_endpoint,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
    if prov == "anthropic":
        actual = model.split("/", 1)[1] if low.startswith("anthropic/") else model
        return AnthropicProvider(
            model=actual,
            api_key=config.anthropic_api_key,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            reasoning_effort=config.reasoning_effort,
        )
    if prov == "groq":
        actual = low.split("groq/", 1)[1] if low.startswith("groq/") else model
        return GroqProvider(
            model=actual,
            api_key=config.groq_api_key,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
    if prov == "deepseek":
        actual = low.split("deepseek/", 1)[1] if low.startswith("deepseek/") else model
        actual = resolve_alias(actual)
        return DeepSeekProvider(
            model=actual,
            api_key=config.deepseek_api_key,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
    if prov == "gemini":
        actual = low.split("gemini/", 1)[1] if low.startswith("gemini/") else model
        actual = resolve_alias(actual)
        return GeminiProvider(
            model=actual,
            api_key=config.gemini_api_key,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
    if prov == "meta":
        actual = low.split("meta/", 1)[1] if low.startswith("meta/") else model
        actual = resolve_alias(actual)
        return MetaProvider(
            model=actual,
            api_key=config.meta_api_key,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            reasoning_effort=config.reasoning_effort,
        )
    if prov == "openai":
        actual = low.split("openai/", 1)[1] if low.startswith("openai/") else model
        actual = resolve_alias(actual)
        return OpenAIProvider(
            model=actual,
            api_key=config.openai_api_key,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            reasoning_effort=config.reasoning_effort,
        )

    raise ValueError(f"Unknown model: {model!r}. Run `coderAI models` to see valid models.")
