"""Model capability hints — port of deepcode core/src/common/model-capabilities.ts."""

from __future__ import annotations

THINKING_CAPABLE_MODELS = {
    "deepseek-v4-pro",
    "gemini-3.7-flash",
    "gpt-5.6-sol",
}

DEEPSEEK_MODELS = {
    "deepseek-v4-flash",
    "deepseek-v4-pro",
}
DEEPSEEK_V4_MODELS = DEEPSEEK_MODELS

MULTIMODAL_MODELS = {
    "gemini-3.7-flash",
    "gpt-5.6-luna",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
}

NON_MULTIMODAL_MODELS = {
    "deepseek-v4-flash",
    "deepseek-v4-pro",
}

FAST_MODELS = {
    "deepseek-v4-flash",
    "gemini-3.7-flash",
    "gpt-5.6-luna",
}


def defaults_to_thinking_mode(model: str) -> bool:
    """Return True if the model defaults to or supports deep thinking/reasoning mode."""
    m = model.strip().lower()
    if m in THINKING_CAPABLE_MODELS:
        return True
    if any(m.startswith(prefix) for prefix in ("o3", "deepseek-reasoner", "deepseek-r1")):
        return True
    return False


def supports_multimodal(model: str, mode: str = "default") -> bool:
    """Whether the given model supports multimodal (image) content.

    `mode` is the resolved `multimodal` configuration:
    - "on": always treat the model as multimodal.
    - "off": always treat the model as non-multimodal.
    - "default" (or omitted): infer from the known model list.
    """
    if mode == "on":
        return True
    if mode == "off":
        return False
    m = model.strip().lower()
    if m in MULTIMODAL_MODELS:
        return True
    if m in NON_MULTIMODAL_MODELS:
        return False
    return m not in NON_MULTIMODAL_MODELS


def is_fast_model(model: str) -> bool:
    """Return True if the model is designed for ultra-low latency / lightweight execution."""
    m = model.strip().lower()
    return m in FAST_MODELS or any(sub in m for sub in ("mini", "flash", "lite", "luna"))


def get_model_badges(model: str) -> list[str]:
    """Return list of capability badges for a given model identifier."""
    badges: list[str] = []
    if defaults_to_thinking_mode(model):
        badges.append("Thinking")
    if is_fast_model(model):
        badges.append("Fast")
    if supports_multimodal(model):
        badges.append("Multimodal")
    return badges


def format_capability_badges(model: str) -> str:
    """Format capability badges as formatted tags, e.g. '[Thinking] [Multimodal]'."""
    badges = get_model_badges(model)
    if not badges:
        return ""
    return " ".join(f"[{b}]" for b in badges)


CURATED_MODELS: list[tuple[str, str, str]] = [
    # OpenAI GPT-5.6
    ("gpt-5.6-sol", "Flagship Tier: Deep reasoning, complex agentic coding", "OpenAI GPT-5.6"),
    ("gpt-5.6-terra", "Balanced Tier: Everyday coding, cost/speed balanced", "OpenAI GPT-5.6"),
    (
        "gpt-5.6-luna",
        "Fast Tier: Ultra-low latency, inline edits & suggestions (Default)",
        "OpenAI GPT-5.6",
    ),
    # Google Gemini
    ("gemini-3.7-flash", "Next-gen hybrid reasoning with visible thinking", "Google Gemini"),
    # DeepSeek V4
    (
        "deepseek-v4-pro",
        "Flagship agentic coding & deep reasoning (1M context)",
        "DeepSeek V4",
    ),
    ("deepseek-v4-flash", "High-throughput coding & tool-calling engine", "DeepSeek V4"),
]
