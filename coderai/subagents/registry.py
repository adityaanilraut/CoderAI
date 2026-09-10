# Ported from coderai/core/subagent_types.py - kimi structure (subagents/registry.py).
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from coderai.subagents.models import BUILTIN_SUBAGENT_TYPES, SubagentTypeDefinition, ToolPolicyMode

@lru_cache(maxsize=1)
def _load_definitions() -> dict[str, SubagentTypeDefinition]:
    """Load builtin types from bundled YAML (cached; ``inherit`` on failure)."""
    defs: dict[str, SubagentTypeDefinition] = {}
    try:
        from coderai.agentspec import DEFAULT_AGENT_FILE, load_agent_spec
    except Exception:
        return defs
    try:
        root = load_agent_spec(DEFAULT_AGENT_FILE)
    except Exception:
        return defs
    for name, ref in root.subagents.items():
        path = ref.get("path", "")
        try:
            from coderai.agentspec import load_agent_spec as _load

            spec = _load(Path(path)) if path else None
        except Exception:
            spec = None
        defs[str(name)] = SubagentTypeDefinition(
            name=str(name),
            description=str(ref.get("description", "")),
            when_to_use=spec.when_to_use if spec else "",
            allowed_tools=tuple(spec.allowed_tools) if spec and spec.allowed_tools else None,
            exclude_tools=tuple(spec.exclude_tools) if spec else (),
        )
    return defs


def list_subagent_types() -> list[SubagentTypeDefinition]:
    """Builtin type definitions (empty when specs are unavailable)."""
    return [d for name in BUILTIN_SUBAGENT_TYPES if (d := _load_definitions().get(name))]


def resolve_tool_policy(
    subagent_type: str | None, requested: list[str] | None = None
) -> tuple[ToolPolicyMode, tuple[str, ...]]:
    """Resolve the effective tool policy for a subagent launch.

    Returns ``(mode, tools)`` where ``inherit`` means no restriction.
    An explicit ``requested`` allowlist always wins.
    """
    if requested:
        return "allowlist", tuple(requested)
    if not subagent_type:
        return "inherit", ()
    definition = _load_definitions().get(subagent_type)
    if definition is None or definition.allowed_tools is None:
        return "inherit", ()
    return "allowlist", definition.allowed_tools


def is_tool_allowed(tool_name: str, mode: ToolPolicyMode, tools: tuple[str, ...]) -> bool:
    """Whether ``tool_name`` may run under a resolved policy."""
    if mode == "inherit":
        return True
    return tool_name in tools


async def build_explore_extra_context(project_root: str) -> str:
    """Git context block for explore prompts (``""`` when unavailable)."""
    try:
        from coderai.subagents.git_context import collect_git_context

        return await collect_git_context(project_root)
    except Exception:
        return ""
