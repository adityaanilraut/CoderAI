"""Thinking-mode request options — port of deepcode core/src/common/openai-thinking.ts."""

from __future__ import annotations


def build_thinking_request_options(
    thinking_enabled: bool,
    base_url: str | None = None,
    reasoning_effort: str = "max",
    model: str = "",
    has_tools: bool = False,
) -> dict:
    m = model.strip().lower()
    is_gpt5 = m.startswith("gpt-5") or "luna" in m or "terra" in m or "sol" in m
    is_reasoning_model = is_gpt5 or m.startswith(("o1", "o3", "deepseek-r1", "deepseek-reasoner"))

    if not thinking_enabled:
        if is_gpt5 and has_tools:
            return {"reasoning_effort": "none"}
        return {}

    if is_reasoning_model:
        effort = (
            reasoning_effort if reasoning_effort in ("low", "medium", "high", "none") else "high"
        )
        return {"reasoning_effort": effort}

    return {
        "extra_body": {
            "reasoning_effort": reasoning_effort,
        }
    }
