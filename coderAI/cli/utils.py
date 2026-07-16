"""Shared CLI utilities."""

from typing import Optional, Set

from coderAI.system.display import Display, display

__all__ = [
    "Display",
    "display",
    "missing_api_key_message",
    "valid_endpoint",
    "valid_models",
]

_NO_API_KEY_MESSAGE = (
    "No API key configured. Run `coderAI setup` to add one, or set a "
    "provider env var (ANTHROPIC_API_KEY, OPENAI_API_KEY, GROQ_API_KEY, "
    "DEEPSEEK_API_KEY, GEMINI_API_KEY). For local models, run `coderAI config set "
    "default_model lmstudio` (or ollama)."
)


def missing_api_key_message() -> Optional[str]:
    """Return an error string if no usable provider is configured, else ``None``.

    A provider is usable when any cloud API key is set, or the default model is
    a local provider (lmstudio/ollama). Shared by ``chat`` and ``run`` so both
    entry points apply the same precheck (with their own output formatting).
    """
    from coderAI.system.config import config_manager

    cfg = config_manager.load()
    has_cloud_key = any(
        [
            getattr(cfg, "openai_api_key", None),
            getattr(cfg, "anthropic_api_key", None),
            getattr(cfg, "groq_api_key", None),
            getattr(cfg, "deepseek_api_key", None),
            getattr(cfg, "gemini_api_key", None),
            getattr(cfg, "meta_api_key", None),
        ]
    )
    local_default = (cfg.default_model or "").lower() in ("lmstudio", "ollama")
    if has_cloud_key or local_default:
        return None
    return _NO_API_KEY_MESSAGE


def valid_models() -> Set[str]:
    """Return the set of valid default-model values accepted by setup()."""
    from coderAI.llm.factory import get_all_model_ids

    return get_all_model_ids()


def valid_endpoint(url: str) -> bool:
    """Loose URL check: must start with http:// or https:// and have a host."""
    from urllib.parse import urlparse

    try:
        p = urlparse(url)
    except Exception:
        return False
    return p.scheme in ("http", "https") and bool(p.netloc)
