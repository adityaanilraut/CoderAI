"""Model capability hints — port of deepcode core/src/common/model-capabilities.ts."""

from __future__ import annotations

THINKING_CAPABLE_MODELS = {
    "gpt-5.6-sol",
    "o3-mini",
    "o1",
    "claude-3-7-sonnet",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "deepseek-v4-pro",
    "deepseek-r1",
    "deepseek-reasoner",
}

DEEPSEEK_V4_MODELS = {
    "deepseek-v4-pro",
    "deepseek-v4-flash",
    "deepseek-chat",
    "deepseek-reasoner",
}

MULTIMODAL_MODELS = {
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-4o",
    "gpt-4o-mini",
    "claude-3-7-sonnet",
    "claude-3-5-sonnet",
    "claude-3-5-haiku",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
}

NON_MULTIMODAL_MODELS = {
    "deepseek-v4-pro",
    "deepseek-v4-flash",
    "deepseek-chat",
    "deepseek-reasoner",
    "deepseek-r1",
    "o3-mini",
    "o1",
}

FAST_MODELS = {
    "gpt-5.6-luna",
    "gpt-4o-mini",
    "claude-3-5-haiku",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "deepseek-v4-flash",
    "o3-mini",
}


def defaults_to_thinking_mode(model: str) -> bool:
    """Return True if the model defaults to or supports deep thinking/reasoning mode."""
    m = model.strip().lower()
    if m in THINKING_CAPABLE_MODELS:
        return True
    if any(m.startswith(prefix) for prefix in ("o1", "o3", "deepseek-r1", "deepseek-reasoner")):
        return True
    return False


def supports_multimodal(model: str) -> bool:
    """Return True if the model supports multimodal / image input."""
    m = model.strip().lower()
    if m in MULTIMODAL_MODELS:
        return True
    if m in NON_MULTIMODAL_MODELS:
        return False
    return m not in NON_MULTIMODAL_MODELS


def is_fast_model(model: str) -> bool:
    """Return True if the model is designed for ultra-low latency / lightweight execution."""
    m = model.strip().lower()
    return m in FAST_MODELS or any(sub in m for sub in ("mini", "flash", "lite", "haiku", "luna"))


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
    # OpenAI GPT-5.6 Tiered Series & Reasoning
    ("gpt-5.6-sol", "Flagship Tier: Deep reasoning, complex agentic coding", "OpenAI GPT-5.6"),
    ("gpt-5.6-terra", "Balanced Tier: Everyday coding, cost/speed balanced", "OpenAI GPT-5.6"),
    (
        "gpt-5.6-luna",
        "Fast Tier: Ultra-low latency, inline edits & suggestions (Default)",
        "OpenAI GPT-5.6",
    ),
    ("o3-mini", "Deep multi-step reasoning with thinking traces", "OpenAI Reasoning"),
    ("o1", "Deep reasoning for complex algorithms", "OpenAI Reasoning"),
    ("gpt-4o", "Standard multimodal legacy tier", "OpenAI Legacy"),
    ("gpt-4o-mini", "Fast, lightweight multimodal legacy tier", "OpenAI Legacy"),
    # Anthropic Claude (Hybrid Reasoning)
    ("claude-3-7-sonnet", "Flagship hybrid reasoning with extended thinking", "Anthropic Claude"),
    ("claude-3-5-sonnet", "Industry-standard coding benchmark leader", "Anthropic Claude"),
    ("claude-3-5-haiku", "High-speed, lightweight sub-agent worker", "Anthropic Claude"),
    # Google Gemini (2.5 Lineup)
    ("gemini-2.5-pro", "2M+ context window, deep logic & repo ingestion", "Google Gemini 2.5"),
    ("gemini-2.5-flash", "Sub-second response with built-in visible thinking", "Google Gemini 2.5"),
    (
        "gemini-2.5-flash-lite",
        "Ultra-efficient lightweight model for high-frequency tooling",
        "Google Gemini 2.5",
    ),
    # DeepSeek (V4 & R1 Reasoning)
    (
        "deepseek-v4-pro",
        "Flagship agentic coding & deep reasoning (1M context)",
        "DeepSeek V4 & R1",
    ),
    ("deepseek-v4-flash", "High-throughput coding & tool-calling engine", "DeepSeek V4 & R1"),
    ("deepseek-r1", "Open reasoning with detailed chain-of-thought", "DeepSeek V4 & R1"),
]
