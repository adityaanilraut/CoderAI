"""Backward-compatible re-export — prefer ``coderAI.prompts.compose``."""

from coderAI.prompts.compose import (  # noqa: F401
    SYSTEM_PROMPT_BROWSER,
    SYSTEM_PROMPT_DESKTOP,
    SYSTEM_PROMPT_INTERACTION,
    SYSTEM_PROMPT_INTRO,
    SYSTEM_PROMPT_OUTPUT_STYLE,
    SYSTEM_PROMPT_RUNTIME,
    SYSTEM_PROMPT_TAIL,
    compose_default_system_prompt,
    format_tools_markdown,
)

__all__ = [
    "SYSTEM_PROMPT_BROWSER",
    "SYSTEM_PROMPT_DESKTOP",
    "SYSTEM_PROMPT_INTERACTION",
    "SYSTEM_PROMPT_INTRO",
    "SYSTEM_PROMPT_OUTPUT_STYLE",
    "SYSTEM_PROMPT_RUNTIME",
    "SYSTEM_PROMPT_TAIL",
    "compose_default_system_prompt",
    "format_tools_markdown",
]
