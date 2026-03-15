"""Utility for tracking API token costs across different models."""

import logging
from typing import Dict

logger = logging.getLogger(__name__)

# Prices per 1,000,000 tokens (USD)
# Source: Public pricing pages as of mid-2024
MODEL_PRICING: Dict[str, Dict[str, float]] = {
    "gpt-5": {"input": 2.50, "output": 10.00},
    "gpt-5-mini": {"input": 0.15, "output": 0.60},
    "gpt-5-nano": {"input": 0.05, "output": 0.40},
    "o1": {"input": 15.00, "output": 60.00},
    "o1-preview": {"input": 15.00, "output": 60.00},
    "o1-mini": {"input": 3.00, "output": 12.00},
    "o3-mini": {"input": 1.10, "output": 4.40},
    "claude-4-sonnet": {"input": 3.00, "output": 15.00},
    "claude-3.7-sonnet": {"input": 3.00, "output": 15.00},
    "claude-3.5-sonnet": {"input": 3.00, "output": 15.00},
    "claude-3.5-haiku": {"input": 0.80, "output": 4.00},
    "claude-3-opus": {"input": 15.00, "output": 75.00},
    "lmstudio": {"input": 0.0, "output": 0.0},
    "ollama": {"input": 0.0, "output": 0.0},
}


class CostTracker:
    """Calculates and tracks API usage costs."""

    def __init__(self):
        """Initialize the cost tracker."""
        self.total_cost_usd: float = 0.0

    def add_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """Calculate cost for a single interaction and add to total.

        Args:
            model: The model name (e.g., 'gpt-5-mini')
            input_tokens: Number of prompt tokens
            output_tokens: Number of completion tokens

        Returns:
            The cost of this specific interaction in USD
        """
        # Normalise model name.
        # Handles dated variants like "claude-3-5-sonnet-20240620" → "claude-3.5-sonnet"
        # and "claude-4-sonnet-20250301" → "claude-4-sonnet".
        base_model = model.lower()

        # Claude dated-model normalisation: strip date suffix, replace hyphens
        # between version digits with dots (e.g. "3-5" → "3.5").
        if base_model.startswith("claude-"):
            # Remove date suffix (e.g. -20240620)
            import re
            base_model = re.sub(r"-\d{8}$", "", base_model)
            # Normalise "claude-3-5-sonnet" → "claude-3.5-sonnet" etc.
            base_model = re.sub(
                r"^(claude-)(\d+)-(\d+)(-\w+)$",
                lambda m: f"{m.group(1)}{m.group(2)}.{m.group(3)}{m.group(4)}",
                base_model,
            )

        # Fallback handling
        pricing = MODEL_PRICING.get(base_model)
        if not pricing:
            # Try partial match
            for k, v in MODEL_PRICING.items():
                if k in base_model:
                    pricing = v
                    break

        if not pricing:
            logger.debug(f"Unknown pricing for model '{model}'. Cost will be 0.")
            return 0.0

        # Calculate cost
        input_cost = (input_tokens / 1_000_000) * pricing["input"]
        output_cost = (output_tokens / 1_000_000) * pricing["output"]
        interaction_cost = input_cost + output_cost

        self.total_cost_usd += interaction_cost
        return interaction_cost

    def get_total_cost(self) -> float:
        """Get the total accumulated cost in USD."""
        return self.total_cost_usd

    @staticmethod
    def format_cost(cost_usd: float) -> str:
        """Format a cost value into a human-readable string."""
        if cost_usd == 0:
            return "$0.00"
        elif cost_usd < 0.01:
            return f"${cost_usd:.4f}"
        else:
            return f"${cost_usd:.2f}"
