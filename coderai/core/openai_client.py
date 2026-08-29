""""""

from __future__ import annotations

import os
from typing import Any

from coderai.core.common.model_capabilities import defaults_to_thinking_mode
from coderai.core.settings import DEFAULT_BASE_URL, resolve_current_settings

# Provider-specific default endpoints
PROVIDER_BASE_URLS = {
    "deepseek": "https://api.deepseek.com",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "openrouter": "https://openrouter.ai/api/v1",
}

_client_pool: dict[str, Any] = {}


def resolve_model_provider_routing(
    model: str,
    explicit_base_url: str | None = None,
    explicit_api_key: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[str, str | None]:
    """Resolve the appropriate baseURL and API key for the selected model.

    Returns (base_url, api_key).
    """
    env = env or {}
    m = model.strip().lower()

    # 1. If user explicitly provided a non-default custom baseURL in settings/env, respect it.
    if explicit_base_url and explicit_base_url != DEFAULT_BASE_URL:
        api_key = (
            explicit_api_key
            or env.get("API_KEY")
            or os.getenv("CODERAI_API_KEY")
            or os.getenv("OPENAI_API_KEY")
        )
        return explicit_base_url, api_key

    # 2. DeepSeek models (deepseek-v4-pro, deepseek-v4-flash, deepseek-r1, deepseek-v3, etc.)
    if m.startswith("deepseek-") or m.startswith("deepseek/"):
        base_url = (
            env.get("DEEPSEEK_BASE_URL")
            or os.getenv("DEEPSEEK_BASE_URL")
            or PROVIDER_BASE_URLS["deepseek"]
        )
        api_key = (
            env.get("DEEPSEEK_API_KEY")
            or os.getenv("DEEPSEEK_API_KEY")
            or explicit_api_key
            or os.getenv("OPENAI_API_KEY")
        )
        return base_url, api_key

    # 3. Google Gemini models (gemini-2.5-pro, gemini-2.5-flash, gemini-2.0-flash, etc.)
    if m.startswith("gemini-") or m.startswith("google/"):
        base_url = (
            env.get("GEMINI_BASE_URL")
            or os.getenv("GEMINI_BASE_URL")
            or PROVIDER_BASE_URLS["gemini"]
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

    # 4. Anthropic Claude models (claude-3-7-sonnet, etc.)
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
                or PROVIDER_BASE_URLS["openrouter"]
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

    # 5. OpenRouter prefix models (openrouter/...)
    if m.startswith("openrouter/") or m.startswith("openrouter-"):
        base_url = (
            env.get("OPENROUTER_BASE_URL")
            or os.getenv("OPENROUTER_BASE_URL")
            or PROVIDER_BASE_URLS["openrouter"]
        )
        api_key = (
            env.get("OPENROUTER_API_KEY")
            or os.getenv("OPENROUTER_API_KEY")
            or explicit_api_key
            or os.getenv("OPENAI_API_KEY")
        )
        return base_url, api_key

    # 6. Default OpenAI / Fallback
    base_url = explicit_base_url or os.getenv("OPENAI_BASE_URL") or DEFAULT_BASE_URL
    api_key = explicit_api_key or os.getenv("OPENAI_API_KEY")
    return base_url, api_key


def create_openai_client(
    project_root: str = ".", model_override: str | None = None
) -> dict[str, Any]:
    global _client_pool
    settings = resolve_current_settings(project_root)
    active_model = model_override or settings["model"]
    configured_key = settings.get("apiKey")
    configured_base_url = settings.get("baseURL")
    env = settings.get("env", {})

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

    def base() -> dict[str, Any]:
        return {
            "client": None,
            "model": active_model,
            "baseURL": base_url,
            "temperature": settings.get("temperature"),
            "thinkingEnabled": thinking_enabled,
            "reasoningEffort": settings.get("reasoningEffort", "max"),
            "debugLogEnabled": settings.get("debugLogEnabled", False),
            "telemetryEnabled": settings.get("telemetryEnabled", False),
            "notify": settings.get("notify"),
            "webSearchTool": settings.get("webSearchTool"),
            "env": env,
        }

    if not api_key:
        return base()

    cache_key = f"{api_key}::{base_url}"
    if cache_key in _client_pool:
        result = base()
        result["client"] = _client_pool[cache_key]
        return result

    try:
        from openai import OpenAI

        try:
            pass
        except Exception:
            pass

        client_instance = OpenAI(api_key=api_key, base_url=base_url or None)
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


def probe_provider_connectivity(
    model: str,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: float = 10.0,
) -> tuple[bool, str]:
    """Probe API connection to a provider with the given model, endpoint, and key.

    Returns (success: bool, message: str).
    """
    resolved_url, resolved_key = resolve_model_provider_routing(
        model=model,
        explicit_base_url=base_url,
        explicit_api_key=api_key,
    )

    if not resolved_key:
        return False, f"No API key provided or resolved for model '{model}' (endpoint: {resolved_url})."

    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=resolved_key,
            base_url=resolved_url or None,
            timeout=timeout,
            max_retries=1,
        )

        # Attempt lightweight probe: try max_completion_tokens first (OpenAI o1/o3/gpt-5 requirement),
        # then fallback to max_tokens, then plain request without token limit parameter.
        resp = None
        probe_errors: list[str] = []

        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "ping"}],
                max_completion_tokens=5,
            )
        except Exception as e_comp:
            probe_errors.append(str(e_comp))
            if any(k in str(e_comp) for k in ("max_completion_tokens", "Unsupported parameter", "400")):
                try:
                    resp = client.chat.completions.create(
                        model=model,
                        messages=[{"role": "user", "content": "ping"}],
                        max_tokens=5,
                    )
                except Exception as e_max:
                    probe_errors.append(str(e_max))
                    try:
                        resp = client.chat.completions.create(
                            model=model,
                            messages=[{"role": "user", "content": "ping"}],
                        )
                    except Exception as e_plain:
                        raise e_plain
            else:
                raise e_comp

        model_name = getattr(resp, "model", model) if resp else model
        return True, f"Successfully connected! Model response received from '{model_name}'."
    except Exception as e:
        # Check if error message has meaningful details
        err_str = str(e)
        if "AuthenticationError" in type(e).__name__ or "401" in err_str:
            return False, "Authentication Failed: Invalid API key (401 Unauthorized)."
        if "NotFoundError" in type(e).__name__ or "404" in err_str:
            return False, f"Model Not Found (404): Endpoint '{resolved_url}' does not recognize '{model}'."
        if "RateLimitError" in type(e).__name__ or "429" in err_str:
            return False, "Rate Limit Exceeded (429): Quota or rate limit reached on provider."
        if "APIConnectionError" in type(e).__name__ or "ConnectError" in err_str:
            return False, f"Connection Failed: Could not reach endpoint '{resolved_url}'."
        return False, f"Connection test error ({type(e).__name__}): {err_str[:120]}"


# Alias for backward compatibility
check_provider_connectivity = probe_provider_connectivity


