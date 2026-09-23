"""Canonical provider registry and provider configuration helpers.

Single source of truth for provider metadata, default model names,
base URLs, and provider key persistence.
"""

from __future__ import annotations

import os
from typing import Any

from coderai.typed_config import load_typed_config, save_typed_config

DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_CONTEXT_WINDOW = 256 * 1024

KNOWN_PROVIDERS: dict[str, dict[str, Any]] = {
    "openai": {
        "name": "OpenAI",
        "env_var": "OPENAI_API_KEY",
        "default_base_url": "https://api.openai.com/v1",
        "default_model": "gpt-6-luna",
        "doc_url": "https://platform.openai.com/api-keys",
        "models": [
            "gpt-6-astra",
            "gpt-6-sol",
            "gpt-6-luna",
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "o3-mini",
            "o1",
            "gpt-4o",
        ],
    },
    "deepseek": {
        "name": "DeepSeek",
        "env_var": "DEEPSEEK_API_KEY",
        "default_base_url": "https://api.deepseek.com",
        "default_model": "deepseek-flash",
        "doc_url": "https://platform.deepseek.com/api_keys",
        "models": ["deepseek-flash", "deepseek-v4-pro", "deepseek-v4-flash"],
    },
    "gemini": {
        "name": "Google Gemini",
        "env_var": "GEMINI_API_KEY",
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "default_model": "gemini-3.7-flash",
        "doc_url": "https://aistudio.google.com/app/apikey",
        "models": ["gemini-3.7-flash", "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"],
    },
    "anthropic": {
        "name": "Anthropic Claude",
        "env_var": "ANTHROPIC_API_KEY",
        "default_base_url": "https://api.anthropic.com/v1",
        "default_model": "claude-3-7-sonnet",
        "doc_url": "https://console.anthropic.com/settings/keys",
        "models": ["claude-3-7-sonnet", "claude-3-5-sonnet"],
    },
    "openrouter": {
        "name": "OpenRouter",
        "env_var": "OPENROUTER_API_KEY",
        "default_base_url": "https://openrouter.ai/api/v1",
        "default_model": "openrouter/anthropic/claude-3.7-sonnet",
        "doc_url": "https://openrouter.ai/keys",
        "models": [
            "openrouter/anthropic/claude-3.7-sonnet",
            "openrouter/deepseek/deepseek-r1",
            "openrouter/meta-llama/llama-3.3-70b-instruct",
        ],
    },
    "jev": {
        "name": "Jev System-One (TypeSafe NAR)",
        "env_var": "TYPESAFE_API_KEY",
        "default_base_url": "",
        "default_model": "jev-system-one",
        "doc_url": "",
        "models": ["jev-system-one"],
    },
}

PROVIDER_REGISTRY = KNOWN_PROVIDERS
"""Canonical cloud provider registry (6 providers).

Single source of truth for provider metadata.
"""


def save_setting_key(key: str, value: Any, scope: str = "user", project_root: str = ".") -> None:
    """Update a specific configuration key in user (~/.coderai) or project settings."""
    from coderai.config import (
        read_project_settings,
        read_settings,
        write_project_settings,
        write_settings,
    )

    if scope == "project":
        current = read_project_settings(project_root) or {}
        current[key] = value
        write_project_settings(current, project_root)
    else:
        current = read_settings() or {}
        current[key] = value
        write_settings(current)


def save_provider_api_key(
    provider: str,
    api_key: str,
    scope: str = "user",
    project_root: str = ".",
) -> str:
    """Save an API key for a specified provider and update os.environ.

    Returns the environment variable name updated.
    """
    from coderai.config import (
        read_project_settings,
        read_settings,
        write_project_settings,
        write_settings,
    )

    prov_key = provider.strip().lower()
    info = KNOWN_PROVIDERS.get(prov_key)
    if info is None:
        known = ", ".join(sorted(KNOWN_PROVIDERS))
        raise ValueError(f"Unknown provider {provider!r}. Known providers: {known}.")
    env_var = str(info["env_var"])
    api_key_clean = api_key.strip()

    # Determine target settings
    if scope == "project":
        current = read_project_settings(project_root) or {}
    else:
        current = read_settings() or {}

    env_dict = dict(current.get("env") or {})
    env_dict[env_var] = api_key_clean

    if prov_key == "openai":
        current["apiKey"] = api_key_clean

    current["env"] = env_dict

    if scope == "project":
        write_project_settings(current, project_root)
    else:
        write_settings(current)

    # Immediately reflect in process env
    os.environ[env_var] = api_key_clean
    if prov_key == "openai":
        os.environ["OPENAI_API_KEY"] = api_key_clean
        os.environ["CODERAI_API_KEY"] = api_key_clean

    return env_var


def save_active_model_setting(
    model: str,
    scope: str = "user",
    project_root: str = ".",
) -> None:
    """Save the default active model in user or project settings."""
    model_clean = model.strip()
    save_setting_key("model", model_clean, scope=scope, project_root=project_root)
    os.environ["CODERAI_MODEL"] = model_clean
    try:
        cfg = load_typed_config()
        target_model = model_clean
        if target_model not in cfg.models:
            for key, m in cfg.models.items():
                if m.model == model_clean:
                    target_model = key
                    break
        cfg.default_model = target_model
        save_typed_config(cfg)
    except (OSError, ValueError):
        pass


def save_base_url_setting(
    base_url: str,
    scope: str = "user",
    project_root: str = ".",
) -> None:
    """Save the baseURL setting in user or project settings."""
    url_clean = base_url.strip()
    save_setting_key("baseURL", url_clean, scope=scope, project_root=project_root)
    os.environ["CODERAI_BASE_URL"] = url_clean


def save_custom_endpoint_config(
    provider_name: str,
    base_url: str,
    api_key: str,
    default_model: str,
    scope: str = "user",
    project_root: str = ".",
) -> None:
    """Configure and persist a custom/local OpenAI-compatible endpoint."""
    from coderai.config import (
        read_project_settings,
        read_settings,
        write_project_settings,
        write_settings,
    )

    if scope == "project":
        current = read_project_settings(project_root) or {}
    else:
        current = read_settings() or {}

    current["baseURL"] = base_url.strip()
    if api_key.strip():
        current["apiKey"] = api_key.strip()
    if default_model.strip():
        current["model"] = default_model.strip()

    env_dict = dict(current.get("env") or {})
    clean_name = provider_name.strip().upper().replace(" ", "_").replace("-", "_")
    if api_key.strip():
        env_dict[f"{clean_name}_API_KEY"] = api_key.strip()
        env_dict["OPENAI_API_KEY"] = api_key.strip()
    if base_url.strip():
        env_dict[f"{clean_name}_BASE_URL"] = base_url.strip()
        env_dict["OPENAI_BASE_URL"] = base_url.strip()

    current["env"] = env_dict

    if scope == "project":
        write_project_settings(current, project_root)
    else:
        write_settings(current)

    # Update process env
    os.environ["CODERAI_BASE_URL"] = base_url.strip()
    os.environ["OPENAI_BASE_URL"] = base_url.strip()
    if api_key.strip():
        os.environ["CODERAI_API_KEY"] = api_key.strip()
        os.environ["OPENAI_API_KEY"] = api_key.strip()
    if default_model.strip():
        os.environ["CODERAI_MODEL"] = default_model.strip()


def get_configured_provider_keys(project_root: str = ".") -> dict[str, dict[str, Any]]:
    """Retrieve key and endpoint status for all known and configured providers."""
    from coderai.config import mask_api_key, resolve_current_settings

    settings = resolve_current_settings(project_root)
    env_map = settings.get("env") or {}
    status_map: dict[str, dict[str, Any]] = {}

    for prov_key, info in KNOWN_PROVIDERS.items():
        var_name = str(info["env_var"])
        key_val = (
            env_map.get(var_name)
            or os.getenv(var_name)
            or (settings.get("apiKey") if prov_key == "openai" else None)
        )
        if not key_val and prov_key == "gemini":
            key_val = env_map.get("GOOGLE_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not key_val and prov_key == "jev":
            key_val = env_map.get("JEV_API_KEY") or os.getenv("JEV_API_KEY")

        is_configured = bool(key_val and key_val.strip())
        status_map[prov_key] = {
            "name": info["name"],
            "env_var": var_name,
            "configured": is_configured,
            "masked_key": mask_api_key(key_val) if is_configured else "Not configured",
            "raw_key": key_val if is_configured else None,
            "default_base_url": info["default_base_url"],
            "default_model": info["default_model"],
            "doc_url": info["doc_url"],
            "models": info["models"],
        }

    # Custom endpoint check
    custom_base_url = settings.get("baseURL")
    if custom_base_url and custom_base_url != DEFAULT_BASE_URL:
        status_map["custom"] = {
            "name": "Custom / Local Endpoint",
            "env_var": "CODERAI_BASE_URL",
            "configured": True,
            "masked_key": mask_api_key(settings.get("apiKey")),
            "raw_key": settings.get("apiKey"),
            "default_base_url": custom_base_url,
            "default_model": settings.get("model", DEFAULT_MODEL),
            "doc_url": "",
            "models": [settings.get("model", DEFAULT_MODEL)],
        }

    return status_map
