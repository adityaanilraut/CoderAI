"""LLM Provider Factory — thin seam over unified registry."""

from typing import Any

from coderAI.llm.registry import (
    ALL_SPECS,
    get_spec,
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
    """Create a provider through the class declared by the model catalog."""
    normalized = model.strip()
    spec = get_spec(normalized)
    if spec is not None:
        provider_cls = spec.provider_cls
    else:
        provider_id = provider_id_for_model(normalized)
        provider_classes = {item.provider: item.provider_cls for item in ALL_SPECS}
        provider_cls = provider_classes[provider_id]
    return provider_cls.from_config(normalized, config)
