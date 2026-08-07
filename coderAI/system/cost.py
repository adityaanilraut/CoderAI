"""Utility for tracking API token costs — prices derived from registry."""

import asyncio
import logging

from coderAI.llm.registry import ALL_SPECS  # generated — edits go in registry.py

logger = logging.getLogger(__name__)

MODEL_PRICING: dict[str, dict[str, float]] = {
    s.id.lower(): {"input": s.input_price, "output": s.output_price} for s in ALL_SPECS
}
# Legacy aliases that should still price correctly (maps to canonical price)
for _s in ALL_SPECS:
    for _a in _s.aliases:
        MODEL_PRICING.setdefault(_a.lower(), {"input": _s.input_price, "output": _s.output_price})
# Additional legacy keys for backward compat (also resolve via CostTracker fallback)
_MODEL_LEGACY_PRICING: dict[str, dict[str, float]] = {
    "gpt-5.6": {"input": 5.00, "output": 30.00},
    "claude-fable-5": {"input": 10.00, "output": 50.00},
    "claude-opus-5": {"input": 5.00, "output": 25.00},
    "claude-sonnet-5": {"input": 3.00, "output": 15.00},
}
for _k, _v in _MODEL_LEGACY_PRICING.items():
    MODEL_PRICING.setdefault(_k, _v)


class CostTracker:
    """Calculates and tracks API usage costs."""

    def __init__(self) -> None:
        self.total_cost_usd: float = 0.0
        self.last_delta_usd: float = 0.0
        self._lock = asyncio.Lock()

    @staticmethod
    def get_model_pricing(model: str) -> dict[str, float]:
        import re as _re
        from coderAI.llm.registry import resolve_alias

        base_model = resolve_alias(model).lower()

        def _strip_date_suffixes(s: str) -> str:
            return _re.sub(r"(-\d{8,})*$", "", s)

        base_model = _strip_date_suffixes(base_model)
        if base_model.startswith("claude-"):
            base_model = _re.sub(
                r"^(claude-)(\d+)-(\d+)(-\w+)$",
                lambda m: f"{m.group(1)}{m.group(2)}.{m.group(3)}{m.group(4)}",
                base_model,
            )
        pricing = MODEL_PRICING.get(base_model)
        if not pricing:
            for k in sorted(MODEL_PRICING, key=len, reverse=True):
                if k in base_model:
                    pricing = MODEL_PRICING[k]
                    break
        if not pricing:
            logger.warning(
                "Unknown pricing for model '%s' (resolved to '%s'). Cost recorded as $0.00",
                model,
                base_model,
            )
            return {"input": 0.0, "output": 0.0, "pricing_known": False}
        return {**pricing, "pricing_known": True}

    @staticmethod
    def calculate_cost_for_tokens(model: str, input_tokens: int, output_tokens: int) -> float:
        pricing = CostTracker.get_model_pricing(model)
        input_cost = (input_tokens / 1_000_000) * pricing["input"]
        output_cost = (output_tokens / 1_000_000) * pricing["output"]
        return input_cost + output_cost

    async def add_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        cost = self.calculate_cost_for_tokens(model, input_tokens, output_tokens)
        async with self._lock:
            self.total_cost_usd += cost
            self.last_delta_usd = cost
        return cost

    def get_total_cost(self) -> float:
        return self.total_cost_usd

    def reset(self) -> None:
        self.total_cost_usd = 0.0
        self.last_delta_usd = 0.0

    @staticmethod
    def format_cost(cost_usd: float) -> str:
        """Format a cost value into a human-readable string."""
        if cost_usd == 0:
            return "$0.00"
        elif cost_usd < 0.01:
            return f"${cost_usd:.4f}"
        else:
            return f"${cost_usd:.2f}"
