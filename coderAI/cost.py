"""Utility for tracking API token costs across different models."""

import logging
from typing import Dict

logger = logging.getLogger(__name__)

# Prices per 1,000,000 tokens (USD)
# Updated: April 2026 (canonical API model IDs)

MODEL_PRICING: Dict[str, Dict[str, float]] = {
    # OpenAI
    "gpt-5.4": {"input": 2.50, "output": 15.00},
    "gpt-5.4-mini": {"input": 0.75, "output": 4.50},
    "gpt-5.4-nano": {"input": 0.20, "output": 1.25},
    "o1": {"input": 15.00, "output": 60.00},
    "o1-pro": {"input": 150.00, "output": 600.00},
    "o3": {"input": 2.00, "output": 8.00},
    "o3-mini": {"input": 1.10, "output": 4.40},

    # Anthropic — Claude 4.X API model IDs
    "claude-opus-4-7": {"input": 5.00, "output": 25.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},

    # Anthropic — Claude 4.X friendly aliases
    "claude-4.7-opus": {"input": 5.00, "output": 25.00},
    "claude-4.6-sonnet": {"input": 3.00, "output": 15.00},
    "claude-4.5-haiku": {"input": 1.00, "output": 5.00},
    "claude-4-sonnet": {"input": 3.00, "output": 15.00},
    "claude-4-opus": {"input": 5.00, "output": 25.00},
    "claude-4-haiku": {"input": 1.00, "output": 5.00},

    # Anthropic — legacy 3.X
    "claude-3-7-sonnet-20250219": {"input": 3.00, "output": 15.00},
    "claude-3-5-sonnet-20241022": {"input": 3.00, "output": 15.00},
    "claude-3-5-haiku-20241022": {"input": 0.80, "output": 4.00},
    "claude-3.7-sonnet": {"input": 3.00, "output": 15.00},
    "claude-3.5-sonnet": {"input": 3.00, "output": 15.00},
    "claude-3.5-haiku": {"input": 0.80, "output": 4.00},

    # Anthropic — short aliases
    "sonnet": {"input": 3.00, "output": 15.00},
    "haiku": {"input": 1.00, "output": 5.00},
    "opus": {"input": 5.00, "output": 25.00},

    # Groq
    "openai/gpt-oss-120b": {"input": 0.15, "output": 0.60},
    "openai/gpt-oss-20b": {"input": 0.075, "output": 0.30},
    "llama3-70b-8192": {"input": 0.59, "output": 0.79},
    "llama3-8b-8192": {"input": 0.05, "output": 0.08},
    "mixtral-8x7b-32768": {"input": 0.27, "output": 0.27},
    "gemma-7b-it": {"input": 0.07, "output": 0.07},
    "llama-3.3-70b-versatile": {"input": 0.59, "output": 0.79},
    "llama-3.1-8b-instant": {"input": 0.05, "output": 0.08},

    # DeepSeek
    "deepseek-v4-flash": {"input": 0.14, "output": 0.28},
    "deepseek-v4-pro": {"input": 1.74, "output": 3.48},
    "deepseek-chat": {"input": 0.27, "output": 1.10},
    "deepseek-reasoner": {"input": 0.55, "output": 2.19},
    "deepseek-v3": {"input": 0.27, "output": 1.10},
    "deepseek-v3.2": {"input": 0.27, "output": 1.10},
    "deepseek-r1": {"input": 0.55, "output": 2.19},

    # Local
    "lmstudio": {"input": 0.0, "output": 0.0},
    "ollama": {"input": 0.0, "output": 0.0},
}



class CostTracker:
    """Calculates and tracks API usage costs."""

    def __init__(self):
        """Initialize the cost tracker."""
        self.total_cost_usd: float = 0.0

    @staticmethod
    def get_model_pricing(model: str) -> Dict[str, float]:
        """Get the pricing for a model (USD per 1M tokens).

        Resolves via: exact key → friendly-alias normalization → longest-substring match.
        Model IDs with date suffixes (e.g. ``claude-haiku-4-5-20251001``) are
        normalised by stripping trailing ``-YYYYMMDD`` segments before lookup.
        """
        base_model = model.lower()

        def _strip_date_suffixes(s: str) -> str:
            """Strip one or more ``-YYYYMMDD`` suffixes from a model ID."""
            import re as _re
            while True:
                stripped = _re.sub(r"-\d{8,}$", "", s)
                if stripped == s:
                    return stripped
                s = stripped

        base_model = _strip_date_suffixes(base_model)

        # Friendly-alias normalisation: claude-4-5-haiku → claude-4.5-haiku
        if base_model.startswith("claude-"):
            import re as _re
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
                "Unknown pricing for model '%s' (resolved to '%s'). "
                "Cost will be recorded as $0.00 — this is likely a new model "
                "that needs a pricing entry in MODEL_PRICING.",
                model, base_model,
            )
            return {"input": 0.0, "output": 0.0}

        return pricing

    @staticmethod
    def calculate_cost_for_tokens(model: str, input_tokens: int, output_tokens: int) -> float:
        """Calculate cost without adding to total."""
        pricing = CostTracker.get_model_pricing(model)
        # ``get_model_pricing`` always returns a dict (zero-cost sentinel when
        # unknown), so ``pricing`` is always truthy here.
        input_cost = (input_tokens / 1_000_000) * pricing["input"]
        output_cost = (output_tokens / 1_000_000) * pricing["output"]
        return input_cost + output_cost

    def add_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """Calculate cost for a single interaction and add to total.

        Args:
            model: The model name (e.g., 'gpt-5.4-mini')
            input_tokens: Number of prompt tokens
            output_tokens: Number of completion tokens

        Returns:
            The cost of this specific interaction in USD
        """
        interaction_cost = self.calculate_cost_for_tokens(model, input_tokens, output_tokens)

        self.total_cost_usd += interaction_cost
        return interaction_cost

    def get_total_cost(self) -> float:
        """Get the total accumulated cost in USD."""
        return self.total_cost_usd

    def reset(self) -> None:
        """Clear the cumulative cost. Called at session boundaries so a fresh
        session is not judged against the previous session's spend."""
        self.total_cost_usd = 0.0

    @staticmethod
    def format_cost(cost_usd: float) -> str:
        """Format a cost value into a human-readable string."""
        if cost_usd == 0:
            return "$0.00"
        elif cost_usd < 0.01:
            return f"${cost_usd:.4f}"
        else:
            return f"${cost_usd:.2f}"
