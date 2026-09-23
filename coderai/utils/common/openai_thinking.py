"""OpenAI-compatible thinking / reasoning-effort request helpers."""

from __future__ import annotations

from typing import Any, Literal

ReasoningEffortLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]

#: Default response field carrying reasoning text.
DEFAULT_REASONING_KEY = "reasoning_content"

_OFF_ALIASES = {"off", "none", "disabled", "false", "0", "disable"}
# GPT-6: Sol/Luna support none/low/medium/high/xhigh/max; Astra supports
# low/medium/high/xhigh/max (no none — low is the minimum).
_OPENAI_EFFORTS = {"none", "low", "medium", "high", "xhigh", "max"}

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
    """Canonicalize effort to off|minimal|low|medium|high|xhigh|max|auto|adaptive."""
    if not reasoning_effort:
        return "max"
    raw = str(reasoning_effort).strip().lower()
    if raw in _OFF_ALIASES:
        return "off"
    if raw in ("auto", "adaptive", "minimal", "low", "medium", "high", "xhigh", "max"):
        return raw
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

    is_gpt = (
        m.startswith("gpt-5")
        or m.startswith("gpt-6")
        or "astra" in m
        or "luna" in m
        or "terra" in m
        or ("sol" in m and "solar" not in m)
    )
    is_openai_reasoning = is_gpt or m.startswith(
        ("o1", "o3", "o4", "deepseek-reasoner", "deepseek-r1")
    )

    # 1. Disabled / Off state (wire uses "none" for GPT Sol/Luna)
    if not thinking_enabled or effort == "off":
        if is_gpt and has_tools:
            # Astra has no "none" — fall back to minimum effort instead.
            if "astra" in m:
                return {"reasoning_effort": "low"}
            return {"reasoning_effort": "none"}
        return {}

    # 2. OpenAI / o-series / GPT-5.6 / GPT-6 (top-level reasoning_effort parameter)
    if is_openai_reasoning:
        # Astra rejects "none"; coerce off->low (already handled above, belt & braces).
        if "astra" in m and effort == "off":
            return {"reasoning_effort": "low"}
        effective_effort = "low" if effort == "minimal" else effort
        if is_gpt:
            # GPT-5.6/6 wire supports none/low/medium/high/xhigh/max.
            openai_effort = effective_effort if effective_effort in _OPENAI_EFFORTS else "high"
            # Internal "off" maps to wire "none" for Sol/Luna.
            if openai_effort == "off":
                openai_effort = "none"
            return {"reasoning_effort": openai_effort}
        # Legacy o-series / DeepSeek-reasoner wire: low/medium/high/none only.
        legacy_efforts = {"none", "low", "medium", "high"}
        openai_effort = effective_effort if effective_effort in legacy_efforts else "high"
        if openai_effort == "off":
            openai_effort = "none"
        return {"reasoning_effort": openai_effort}

    # 3. Extra body for DeepSeek, Gemini, OpenRouter, Qwen
    return {
        "extra_body": {
            "reasoning_effort": effort,
        }
    }


def extract_reasoning_content(message: Any, reasoning_key: str | None = None) -> Any:
    """Read reasoning text from a response message via the provider's key.

    ``reasoning_key`` (default ``"reasoning_content"``) selects
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
        from coderai.config import load_typed_config

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
