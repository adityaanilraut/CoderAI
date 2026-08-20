"""Thinking-mode request options — port of deepcode core/src/common/openai-thinking.ts."""

from __future__ import annotations

_OFF_ALIASES = {"off", "none", "disabled", "false", "0"}
_OPENAI_EFFORTS = {"low", "medium", "high", "none"}


def normalize_reasoning_effort(reasoning_effort: str | None) -> str:
    """Canonicalize effort to off|low|medium|high|max. Unknown values become max."""
    raw = (reasoning_effort or "max").strip().lower()
    if raw in _OFF_ALIASES:
        return "off"
    if raw in ("low", "medium", "high", "max"):
        return raw
    return "max"


def build_thinking_request_options(
    thinking_enabled: bool,
    base_url: str | None = None,
    reasoning_effort: str = "max",
    model: str = "",
    has_tools: bool = False,
) -> dict:
    del base_url
    m = model.strip().lower()
    is_gpt5 = m.startswith("gpt-5") or "luna" in m or "terra" in m or "sol" in m
    is_reasoning_model = is_gpt5 or m.startswith(("o1", "o3", "deepseek-reasoner", "deepseek-r1"))
    effort = normalize_reasoning_effort(reasoning_effort)

    if not thinking_enabled or effort == "off":
        if is_gpt5 and has_tools:
            return {"reasoning_effort": "none"}
        return {}

    if is_reasoning_model:
        # OpenAI-style top-level reasoning_effort has no `max`; map it to high.
        openai_effort = effort if effort in _OPENAI_EFFORTS else "high"
        return {"reasoning_effort": openai_effort}

    return {
        "extra_body": {
            "reasoning_effort": effort,
        }
    }
