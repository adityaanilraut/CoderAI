""""""

from __future__ import annotations

THINKING_CAPABLE_MODELS = {
    "deepseek-flash",
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "gemini-3.7-flash",
    "gpt-5.6-sol",
    "gpt-6-sol",
    "gpt-6-astra",
    "kimi-for-coding",
    "kimi-code",
    "kimi-k2.5",
}

DEEPSEEK_MODELS = {
    "deepseek-flash",
    "deepseek-v4-flash",
    "deepseek-v4-pro",
}
DEEPSEEK_V4_MODELS = DEEPSEEK_MODELS

MULTIMODAL_MODELS = {
    "deepseek-flash",
    "deepseek-v4-flash",
    "gemini-3.7-flash",
    "gpt-5.6-luna",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-6-astra",
    "gpt-6-sol",
    "gpt-6-luna",
    "kimi-for-coding",
    "kimi-code",
    "kimi-k2.5",
    "kimi-k1.5",
}

NON_MULTIMODAL_MODELS = {
    "deepseek-v4-pro",
}

FAST_MODELS = {
    "deepseek-flash",
    "deepseek-v4-flash",
    "gemini-3.7-flash",
    "gpt-5.6-luna",
    "gpt-6-luna",
    "kimi-k1.5",
    "jev-system-one",
}

#: Non-autoregressive System-One models (single-shot triage/gating, no tools/streaming).
JEV_MODELS = frozenset({"jev-system-one"})

ALL_REASONING_EFFORTS = ["off", "low", "medium", "high", "xhigh", "max"]


def is_jev_model(model: str) -> bool:
    """Return True if the model id routes to the Jev System-One NAR backend."""
    m = (model or "").strip().lower()
    return m in JEV_MODELS or m.startswith("jev-") or m.startswith("typesafe")


def defaults_to_thinking_mode(model: str) -> bool:
    """Return True if the model defaults to deep thinking/reasoning mode."""
    m = model.strip().lower()
    if is_jev_model(m):
        return False
    if m in ("gpt-5.6-luna", "gpt-6-luna", "kimi-k1.5"):
        return False
    if m in THINKING_CAPABLE_MODELS:
        return True
    if (
        any(
            m.startswith(prefix)
            for prefix in (
                "o1",
                "o3",
                "o4",
                "deepseek-reasoner",
                "deepseek-r1",
                "deepseek-flash",
                "deepseek-v4-flash",
                "claude-3-7",
                "deepseek-v4-pro",
                "kimi",
            )
        )
        or "thinking" in m
        or "reason" in m
        or "kimi-code" in m
        or "kimi-for-coding" in m
    ):
        return True
    return False


def get_supported_reasoning_efforts(model: str) -> list[str]:
    """Return the list of supported reasoning efforts for a given model."""
    m = model.strip().lower()
    # GPT-6 Astra has no "off"/"none" — low is the minimum effort.
    if "astra" in m or m == "gpt-6-astra":
        return ["low", "medium", "high", "xhigh", "max"]
    # GPT-6 Sol/Luna support none(off) + xhigh; GPT-5.6 family kept for compat.
    if "gpt-6" in m or "gpt-5.6" in m:
        return ["off", "low", "medium", "high", "xhigh", "max"]
    if (
        defaults_to_thinking_mode(model)
        or m
        in (
            "deepseek-flash",
            "deepseek-v4-flash",
            "gemini-3.7-flash",
            "deepseek-v4-pro",
        )
        or "kimi" in m
    ):
        return ["off", "low", "medium", "high", "xhigh", "max"]
    return ["off"]


def get_default_reasoning_effort(model: str) -> str:
    """Return the recommended default reasoning effort for a given model."""
    m = model.strip().lower()
    if not defaults_to_thinking_mode(m):
        return "off"
    if "astra" in m or "sol" in m or "pro" in m or "r1" in m or "o3" in m or "o1" in m:
        return "max"
    if "terra" in m or "medium" in m:
        return "medium"
    if "luna" in m or "flash" in m:
        return "low"
    return "max"


def resolve_adaptive_reasoning_effort(
    model: str,
    turn: int = 1,
    step: int = 1,
    explicit_effort: str | None = None,
) -> str:
    """Dynamically resolve reasoning effort to minimize latency on iterative steps.

    If explicit_effort is provided and not in ('adaptive', 'auto', None, ''), respect it.
    For fast/flash thinking models (e.g. deepseek-flash, gemini-3.7-flash, gpt-6-luna):
    - Turn 1 / Step 1: use 'high' or 'max' for initial planning & root-cause reasoning.
    - Iterative tool execution steps (Turn > 1 or Step > 1): use 'low' (or 'medium')
      to avoid 500+ token reasoning stalls during routine inspection and file updates.
    """
    m = model.strip().lower()
    if explicit_effort and explicit_effort not in ("adaptive", "auto"):
        return explicit_effort

    if is_fast_model(m) or "flash" in m or "luna" in m:
        if turn <= 1 and step <= 1:
            return "high"
        return "low"

    if turn <= 1 and step <= 1:
        return "max"
    return "high"


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
    if is_jev_model(m):
        return True
    return m in FAST_MODELS or any(sub in m for sub in ("mini", "flash", "lite", "luna"))


def get_model_badges(model: str) -> list[str]:
    """Return list of capability badges for a given model identifier."""
    badges: list[str] = []
    if is_jev_model(model):
        badges.append("System-One")
        badges.append("Fast")
        return badges
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


def format_model_effort_tags(model: str) -> str:
    """Format available effort tags as a summary string, e.g. 'Efforts: low, medium, high, max'."""
    efforts = get_supported_reasoning_efforts(model)
    if efforts == ["off"]:
        return "Effort: off"
    active_efforts = [e for e in efforts if e != "off"]
    return f"Efforts: {', '.join(active_efforts)}"


CURATED_MODELS: list[tuple[str, str, str]] = [
    # OpenAI GPT-6 (current generation, Sept 2026)
    (
        "gpt-6-astra",
        "Flagship Tier: Most intelligent & aligned, hardest end-to-end work (Effort: max)",
        "OpenAI GPT-6",
    ),
    (
        "gpt-6-sol",
        "Balanced Tier: Complex coding & agentic workflows, $2/$10 per 1M (Effort: max)",
        "OpenAI GPT-6",
    ),
    (
        "gpt-6-luna",
        "Fast Tier: Ultra-low latency high-volume tasks, $0.10/$0.50 per 1M (Default)",
        "OpenAI GPT-6",
    ),
    # OpenAI GPT-5.6 (legacy, kept for compat — no GPT-6 Terra exists)
    (
        "gpt-5.6-sol",
        "Legacy Flagship: Deep reasoning, complex agentic coding (Effort: max)",
        "OpenAI GPT-5.6",
    ),
    (
        "gpt-5.6-terra",
        "Legacy Balanced: Everyday coding, cost/speed balanced (Effort: medium)",
        "OpenAI GPT-5.6",
    ),
    (
        "gpt-5.6-luna",
        "Legacy Fast: Ultra-low latency, inline edits & suggestions",
        "OpenAI GPT-5.6",
    ),
    # Kimi Code / Moonshot
    (
        "kimi-code/kimi-for-coding",
        "Kimi Code Flagship: Frontier agentic coding with thinking & multimodal",
        "Kimi Code",
    ),
    (
        "kimi-for-coding",
        "Kimi Code: Direct flagship coding model",
        "Kimi Code",
    ),
    (
        "kimi-k2.5",
        "Kimi K2.5: High capability reasoning & coding model",
        "Kimi / Moonshot",
    ),
    (
        "kimi-k1.5",
        "Kimi K1.5: Fast reasoning & 128k long context",
        "Kimi / Moonshot",
    ),
    # Google Gemini
    (
        "gemini-3.7-flash",
        "Next-gen hybrid reasoning with visible thinking (Efforts: low..max)",
        "Google Gemini",
    ),
    # DeepSeek V4.1 / V4
    (
        "deepseek-flash",
        "Latest V4.1 Flash: thinking + vision, 1M context, high-throughput coding (Default)",
        "DeepSeek",
    ),
    (
        "deepseek-v4-pro",
        "Flagship agentic coding & deep reasoning 1M context (Effort: max)",
        "DeepSeek V4",
    ),
    (
        "deepseek-v4-flash",
        "Legacy alias — served by deepseek-flash (V4.1), prefer deepseek-flash",
        "DeepSeek V4",
    ),
    # Anthropic Claude
    ("claude-3-7-sonnet", "Hybrid reasoning & deep frontier tool calling", "Anthropic"),
    # Jev System-One is deliberately absent: this list feeds chat-model pickers, and a
    # non-autoregressive triage model cannot drive the tool loop.
]
