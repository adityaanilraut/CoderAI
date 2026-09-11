"""OpenAI-compatible thinking / reasoning-effort request helpers."""

from __future__ import annotations

from typing import Any, Literal

ReasoningEffortLevel = Literal["off", "minimal", "low", "medium", "high", "max"]

#: Default response field carrying reasoning text (Kimi ``reasoning_key`` parity).
DEFAULT_REASONING_KEY = "reasoning_content"

_OFF_ALIASES = {"off", "none", "disabled", "false", "0", "disable"}
_OPENAI_EFFORTS = {"low", "medium", "high", "none"}

# Provider-specific thinking token budgets mapped from effort levels
GEMINI_THINKING_BUDGETS: dict[str, int] = {
    "minimal": 1024,
    "low": 2048,
    "medium": 8192,
    "high": 24576,
    "max": 65536,
}

ANTHROPIC_THINKING_BUDGETS: dict[str, int] = {
    "minimal": 1024,
    "low": 1024,
    "medium": 4096,
    "high": 16384,
    "max": 32768,
}


def normalize_reasoning_effort(reasoning_effort: str | None) -> str:
    """Canonicalize effort to off|minimal|low|medium|high|max. Unknown values become max."""
    if not reasoning_effort:
        return "max"
    raw = str(reasoning_effort).strip().lower()
    if raw in _OFF_ALIASES:
        return "off"
    if raw in ("minimal", "low", "medium", "high", "max"):
        return raw
    if raw == "xhigh":
        return "max"
    return "max"


def get_thinking_token_budget(effort: str, provider: str = "generic") -> int:
    """Return recommended thinking token budget for a given provider and effort level."""
    canon = normalize_reasoning_effort(effort)
    if canon == "off":
        return 0
    p = provider.strip().lower()
    if "gemini" in p or "google" in p:
        return GEMINI_THINKING_BUDGETS.get(canon, 24576)
    if "anthropic" in p or "claude" in p:
        return ANTHROPIC_THINKING_BUDGETS.get(canon, 16384)
    # Default fallback budget
    fallback_budgets = {
        "minimal": 1024,
        "low": 2048,
        "medium": 8192,
        "high": 16384,
        "max": 32768,
    }
    return fallback_budgets.get(canon, 32768)


def build_thinking_request_options(
    thinking_enabled: bool,
    base_url: str | None = None,
    reasoning_effort: str = "max",
    model: str = "",
    has_tools: bool = False,
) -> dict:
    """Build provider-appropriate thinking and reasoning-effort options for OpenAI-compatible client calls."""
    del base_url
    m = model.strip().lower()
    effort = normalize_reasoning_effort(reasoning_effort)

    is_gpt5 = m.startswith("gpt-5") or "luna" in m or "terra" in m or "sol" in m
    is_openai_reasoning = is_gpt5 or m.startswith(
        ("o1", "o3", "o4", "deepseek-reasoner", "deepseek-r1")
    )

    # 1. Disabled / Off state
    if not thinking_enabled or effort == "off":
        if is_gpt5 and has_tools:
            return {"reasoning_effort": "none"}
        return {}

    # 2. OpenAI / o-series / GPT-5.6 (top-level reasoning_effort parameter)
    if is_openai_reasoning:
        openai_effort = effort if effort in _OPENAI_EFFORTS else "high"
        return {"reasoning_effort": openai_effort}

    # 3. Extra body for DeepSeek, Gemini, OpenRouter, Qwen
    return {
        "extra_body": {
            "reasoning_effort": effort,
        }
    }


def extract_reasoning_content(message: Any, reasoning_key: str | None = None) -> Any:
    """Read reasoning text from a response message via the provider's key.

    Kimi parity: ``reasoning_key`` (default ``"reasoning_content"``) selects
    the response field, so providers that deviate from the OpenAI convention
    still surface their thinking trace. Accepts dicts and objects.
    """
    key = (reasoning_key or "").strip() or DEFAULT_REASONING_KEY
    if isinstance(message, dict):
        return message.get(key)
    return getattr(message, key, None)


def resolve_reasoning_key(client_info: dict[str, Any] | None = None) -> str:
    """Resolve the active reasoning key: client info → typed provider → default."""
    if isinstance(client_info, dict):
        key = str(client_info.get("reasoningKey") or "").strip()
        if key:
            return key
    return DEFAULT_REASONING_KEY


def reasoning_key_for_model(model: str) -> str:
    """Look up the typed provider's reasoning key for a model (cheap, no client)."""
    try:
        from coderai.core.typed_config import load_typed_config

        typed = load_typed_config()
        for key, m in typed.models.items():
            if key == model or m.model == model:
                provider = typed.providers.get(m.provider)
                if provider is not None and provider.reasoning_key:
                    return provider.reasoning_key
                break
    except Exception:
        pass
    return DEFAULT_REASONING_KEY
