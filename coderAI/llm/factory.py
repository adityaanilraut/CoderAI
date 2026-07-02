"""LLM Provider Factory.

Also the single seam for model-alias resolution and model listings so callers
outside ``coderAI.llm`` never import a specific provider module.
"""

from typing import Any, Dict, List, Set, Tuple
from coderAI.llm.openai import OpenAIProvider
from coderAI.llm.anthropic import AnthropicProvider, MODEL_ALIASES as ANTHROPIC_MODEL_ALIASES
from coderAI.llm.lmstudio import LMStudioProvider
from coderAI.llm.ollama import OllamaProvider
from coderAI.llm.groq import GroqProvider
from coderAI.llm.deepseek import DeepSeekProvider
from coderAI.llm.gemini import GeminiProvider

# Aggregated friendly-name → canonical-model-ID map, collected from the provider
# modules that expose one (only Anthropic today). Adding a provider with its own
# alias table is a one-line merge here.
_MODEL_ALIASES: Dict[str, str] = {**ANTHROPIC_MODEL_ALIASES}


def resolve_model_alias(name: str) -> str:
    """Resolve a friendly model alias to its canonical ID.

    Unknown names (and non-strings) pass through unchanged so callers can hand
    off arbitrary user input safely.
    """
    if not isinstance(name, str):
        return name
    return _MODEL_ALIASES.get(name.lower(), name)


def get_all_model_ids() -> Set[str]:
    """Return the set of all recognised model identifiers across all providers."""
    return (
        set(OpenAIProvider.SUPPORTED_MODELS.keys())
        | set(ANTHROPIC_MODEL_ALIASES.keys())
        | set(GroqProvider.SUPPORTED_MODELS.keys())
        | set(DeepSeekProvider.SUPPORTED_MODELS.keys())
        | set(GeminiProvider.SUPPORTED_MODELS.keys())
        | {"lmstudio", "ollama"}
    )


def get_models_by_provider() -> List[Tuple[str, List[str], str]]:
    """Return ``(provider label, model names, requirement)`` groups for display.

    Centralises the per-provider model listings so CLI/UI code doesn't import
    individual provider modules just to enumerate their models.
    """
    return [
        ("OpenAI Provider", list(OpenAIProvider.SUPPORTED_MODELS.keys()), "OpenAI API key"),
        ("Anthropic Provider", list(ANTHROPIC_MODEL_ALIASES.keys()), "Anthropic API key"),
        ("Groq Provider", list(GroqProvider.SUPPORTED_MODELS.keys()), "Groq API key"),
        ("DeepSeek Provider", list(DeepSeekProvider.SUPPORTED_MODELS.keys()), "DeepSeek API key"),
        ("Gemini Provider", list(GeminiProvider.SUPPORTED_MODELS.keys()), "Gemini API key"),
        ("LM Studio Provider", ["lmstudio"], "LM Studio running locally"),
        ("Ollama Provider", ["ollama"], "Ollama running locally"),
    ]


def create_provider(model: str, config: Any) -> Any:
    """Create LLM provider based on model.

    Raises ``ValueError`` when ``model`` is not recognized by any provider.
    We intentionally do *not* fall back to OpenAI: a typo in
    ``CODERAI_DEFAULT_MODEL`` should fail loudly rather than silently
    routing traffic (and keys) to the wrong backend.
    """
    model_lower = model.lower()
    if model_lower == "ollama" or model_lower.startswith("ollama/"):
        actual_model = (
            model.split("/", 1)[1] if model.startswith("ollama/") else config.ollama_model
        )
        return OllamaProvider(
            model=actual_model,
            endpoint=config.ollama_endpoint,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
    if model_lower == "lmstudio" or model_lower.startswith("lmstudio/"):
        actual_model = (
            model.split("/", 1)[1] if model.startswith("lmstudio/") else config.lmstudio_model
        )
        return LMStudioProvider(
            model=actual_model,
            endpoint=config.lmstudio_endpoint,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
    if (
        model_lower.startswith("claude")
        or model_lower.startswith("anthropic/")
        or model_lower in ANTHROPIC_MODEL_ALIASES
        or model_lower in ("sonnet", "opus", "haiku")
    ):
        actual_model = (
            model.split("anthropic/", 1)[1] if model_lower.startswith("anthropic/") else model
        )
        return AnthropicProvider(
            model=actual_model,
            api_key=config.anthropic_api_key,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            reasoning_effort=config.reasoning_effort,
        )
    if model_lower in GroqProvider.SUPPORTED_MODELS or model_lower.startswith("groq/"):
        actual_model = (
            model_lower.split("groq/", 1)[1] if model_lower.startswith("groq/") else model
        )
        return GroqProvider(
            model=actual_model,
            api_key=config.groq_api_key,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
    if model_lower in (
        k.lower() for k in DeepSeekProvider.SUPPORTED_MODELS
    ) or model_lower.startswith("deepseek/"):
        actual_model = (
            model_lower.split("deepseek/", 1)[1] if model_lower.startswith("deepseek/") else model
        )
        return DeepSeekProvider(
            model=actual_model,
            api_key=config.deepseek_api_key,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
    if model_lower in GeminiProvider.SUPPORTED_MODELS or model_lower.startswith(
        ("gemini-", "gemini/")
    ):
        actual_model = (
            model_lower.split("gemini/", 1)[1] if model_lower.startswith("gemini/") else model
        )
        return GeminiProvider(
            model=actual_model,
            api_key=config.gemini_api_key,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
    if model_lower in OpenAIProvider.SUPPORTED_MODELS or model_lower.startswith(
        ("o1-", "o3", "gpt-", "openai/")
    ):
        actual_model = (
            model_lower.split("openai/", 1)[1] if model_lower.startswith("openai/") else model
        )
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
