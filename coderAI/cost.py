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
    "gpt-5.4": {"input": 1.25, "output": 10.00},
    "gpt-5.4-nano": {"input": 0.05, "output": 0.40},
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
    "llama-3.3-70b-versatile": {"input": 0.59, "output": 0.79},
    "llama-3.1-8b-instant": {"input": 0.05, "output": 0.08},

    # DeepSeek
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

    def add_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """Calculate cost for a single interaction and add to total.

        Args:
            model: The model name (e.g., 'gpt-5.4-mini')
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
