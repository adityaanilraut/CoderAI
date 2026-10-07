"""Effective budgets and per-model accounting shared by shell displays."""

from typing import Any


def context_limit(mgr: Any) -> int:
    from coderai.prompt import calculate_context_budget

    settings = mgr.get_resolved_settings()
    configured = settings.get("contextWindow") if isinstance(settings, dict) else None
    return calculate_context_budget(mgr.get_active_model(), context_limit=configured)[
        "context_limit"
    ]


def session_cost(entry: Any, model: str) -> float | None:
    from coderai.ui.shell.session_picker import estimate_model_cost

    rows = getattr(entry, "usage_per_model", None) or {model: getattr(entry, "usage", None) or {}}
    costs = [
        estimate_model_cost(
            name,
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            usage.get("cached_tokens", 0),
        )
        for name, usage in rows.items()
    ]
    if any(cost is None for cost in costs):
        return None
    return sum(cost for cost in costs if cost is not None)


def cost_text(cost: float | None) -> str:
    return f"${cost:.4f} USD" if cost is not None else "Unavailable (unknown model pricing)"


def token_source(mgr: Any, session_id: str) -> str:
    """Expose historical unknowns instead of silently calling estimates reported."""
    sources = {
        (message.meta or {}).get("usageSource", "unspecified (older record)")
        for message in mgr.list_session_messages(session_id)
        if (message.meta or {}).get("usage")
    }
    if not sources:
        return "Unavailable"
    if len(sources) == 1:
        return next(iter(sources))
    return "Mixed: " + ", ".join(sorted(sources))
