"""Shared CLI utilities."""

from typing import Optional

from coderAI.system.display import Display, display

__all__ = [
    "Display",
    "display",
    "is_valid_model",
    "missing_api_key_message",
    "valid_endpoint",
]

_PROVIDER_KEYS = {
    "openai": ("OpenAI", "openai_api_key", "OPENAI_API_KEY"),
    "anthropic": ("Anthropic", "anthropic_api_key", "ANTHROPIC_API_KEY"),
    "groq": ("Groq", "groq_api_key", "GROQ_API_KEY"),
    "deepseek": ("DeepSeek", "deepseek_api_key", "DEEPSEEK_API_KEY"),
    "gemini": ("Gemini", "gemini_api_key", "GEMINI_API_KEY"),
    "meta": ("Meta", "meta_api_key", "MODEL_API_KEY or META_API_KEY"),
}


def missing_api_key_message(model: Optional[str] = None) -> Optional[str]:
    """Return an error if the selected model's provider lacks its credential."""
    from coderAI.system.config import config_manager
    from coderAI.llm.factory import provider_id_for_model

    cfg = config_manager.load()
    selected = (model or cfg.default_model or "").strip()
    try:
        provider_id = provider_id_for_model(selected)
    except ValueError as exc:
        return str(exc)
    if provider_id in ("lmstudio", "ollama"):
        return None
    label, config_key, env_var = _PROVIDER_KEYS[provider_id]
    if getattr(cfg, config_key, None):
        return None
    return (
        f"{label} API key is required for model '{selected}'. Set {env_var}, "
        f"run `coderAI config set {config_key} <YOUR_KEY>`, or choose another model."
    )


def is_valid_model(model: str) -> bool:
    """Return whether the provider factory accepts this model identifier."""
    from coderAI.llm.factory import is_valid_model_name

    return is_valid_model_name(model)


def valid_endpoint(url: str) -> bool:
    """Loose URL check: must start with http:// or https:// and have a host."""
    from urllib.parse import urlparse

    try:
        p = urlparse(url)
    except Exception:
        return False
    return p.scheme in ("http", "https") and bool(p.netloc)
