"""Agent specification loading with ``extend`` inheritance.

Specs are small YAML files declaring the system prompt, tools, and subagents
for an agent flavor (``default`` + ``okabe`` bundled under
``coderai/agents/``). A spec may ``extend`` another file; scalar fields merge
towards the child, ``system_prompt_args`` merge key-wise.

The builtin ``default`` spec uses CoderAI tool names (``bash``, ``read``,
``edit/write``...).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from coderai.exception import AgentSpecError

DEFAULT_AGENT_SPEC_VERSION = "1"
SUPPORTED_AGENT_SPEC_VERSIONS = (DEFAULT_AGENT_SPEC_VERSION,)


def get_agents_dir() -> Path:
    """Directory containing bundled agent flavors."""
    return Path(__file__).parent / "agents"


DEFAULT_AGENT_FILE = get_agents_dir() / "default" / "agent.yaml"
OKABE_AGENT_FILE = get_agents_dir() / "okabe" / "agent.yaml"


class Inherit(NamedTuple):
    """Marker for fields inherited from the extended spec."""


inherit = Inherit()


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:
        raise AgentSpecError(f"Agent specs require PyYAML, which is not installed: {path}") from exc
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise AgentSpecError(f"Invalid YAML in agent spec file: {e}") from e
    if not isinstance(data, dict):
        raise AgentSpecError(f"Invalid agent spec file (top-level mapping required): {path}")
    return data


@dataclass(frozen=True, slots=True, kw_only=True)
class ResolvedAgentSpec:
    """Fully-resolved agent specification (no ``extend`` left)."""

    name: str
    system_prompt_path: Path
    system_prompt_args: dict[str, str]
    model: str | None
    when_to_use: str
    tools: list[str]
    allowed_tools: list[str] | None
    exclude_tools: list[str]
    subagents: dict[str, dict[str, str]]


def _resolve_dict(raw: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    """Recursively resolve ``extend`` chains into a single merged mapping."""
    version = str(raw.get("version", DEFAULT_AGENT_SPEC_VERSION))
    if version not in SUPPORTED_AGENT_SPEC_VERSIONS:
        raise AgentSpecError(f"Unsupported agent spec version: {version}")
    agent = dict(raw.get("agent", {}) or {})

    extend = agent.get("extend")
    if not extend:
        return agent
    if extend == "default":
        base_file = DEFAULT_AGENT_FILE
    else:
        base_file = (base_dir / str(extend)).resolve()
    if not base_file.is_file():
        raise AgentSpecError(f"Extended agent spec not found: {base_file}")
    base = _resolve_dict(_load_yaml(base_file), base_file.parent)

    merged = dict(base)
    for key, value in agent.items():
        if key == "extend":
            continue
        if key == "system_prompt_args":
            merged_args = dict(base.get("system_prompt_args") or {})
            merged_args.update(value or {})
            merged[key] = merged_args
        elif value is not None:
            merged[key] = value
    return merged


def load_agent_spec(agent_file: Path) -> ResolvedAgentSpec:
    """Load and fully resolve an agent spec file.

    Raises:
        AgentSpecError: If the file is missing, invalid, or leaves required
            fields unresolved.
    """
    agent_file = Path(agent_file)
    if not agent_file.exists():
        raise AgentSpecError(f"Agent spec file not found: {agent_file}")
    if not agent_file.is_file():
        raise AgentSpecError(f"Agent spec path is not a file: {agent_file}")

    agent = _resolve_dict(_load_yaml(agent_file), agent_file.parent)

    name = agent.get("name")
    if not name:
        raise AgentSpecError("Agent name is required")
    prompt_rel = agent.get("system_prompt_path")
    if not prompt_rel:
        raise AgentSpecError("System prompt path is required")
    system_prompt_path = (agent_file.parent / str(prompt_rel)).resolve()
    if not system_prompt_path.is_file() and DEFAULT_AGENT_FILE.is_file():
        fallback_prompt = (DEFAULT_AGENT_FILE.parent / str(prompt_rel)).resolve()
        if fallback_prompt.is_file():
            system_prompt_path = fallback_prompt
    tools = agent.get("tools")
    if tools is None:
        raise AgentSpecError("Tools are required")

    subagents_raw = agent.get("subagents") or {}
    subagents: dict[str, dict[str, str]] = {}
    for key, spec in subagents_raw.items():
        if not isinstance(spec, dict):
            raise AgentSpecError(f"Invalid subagent spec for {key!r}")
        sub_path = spec.get("path", "")
        subagents[str(key)] = {
            "path": str((agent_file.parent / str(sub_path)).resolve()) if sub_path else "",
            "description": str(spec.get("description", "")),
        }

    return ResolvedAgentSpec(
        name=str(name),
        system_prompt_path=system_prompt_path,
        system_prompt_args={
            str(k): str(v) for k, v in (agent.get("system_prompt_args") or {}).items()
        },
        model=agent.get("model"),
        when_to_use=str(agent.get("when_to_use") or ""),
        tools=[str(t) for t in (tools or [])],
        allowed_tools=[str(t) for t in agent["allowed_tools"]]
        if agent.get("allowed_tools") is not None
        else None,
        exclude_tools=[str(t) for t in (agent.get("exclude_tools") or [])],
        subagents=subagents,
    )


def render_system_prompt(spec: ResolvedAgentSpec, extra_args: dict[str, str] | None = None) -> str:
    """Render the spec's system-prompt template with ``${VAR}`` substitution.

    Raises:
        AgentSpecError: If the template file is missing.
    """
    from coderai.exception import SystemPromptTemplateError

    if not spec.system_prompt_path.is_file():
        raise AgentSpecError(f"System prompt file not found: {spec.system_prompt_path}")
    template = spec.system_prompt_path.read_text(encoding="utf-8")
    if template.startswith("---"):
        parts = template.split("---", 2)
        if len(parts) >= 3:
            template = parts[2].strip()
    args = dict(spec.system_prompt_args)
    if extra_args:
        args.update(extra_args)
    try:
        import re

        def _replace(match: re.Match[str]) -> str:
            key = match.group(1)
            return args.get(key, match.group(0))

        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", _replace, template)
    except Exception as exc:
        raise SystemPromptTemplateError(f"Failed to render system prompt: {exc}") from exc


def resolve_agent_spec(
    name_or_path: str | Path,
    project_root: Path | None = None,
) -> ResolvedAgentSpec:
    """Resolve an agent spec from a bundled flavor name, a file path (.yaml or .md),
    or a discovered role name from .coderai/agents/*.md.
    """
    path_obj = Path(name_or_path)
    # Check direct file path
    if path_obj.is_file():
        if path_obj.suffix.lower() in (".yaml", ".yml"):
            return load_agent_spec(path_obj)
        if path_obj.suffix.lower() == ".md":
            from coderai.subagents.registry import _parse_markdown_agent_file

            defn = _parse_markdown_agent_file(path_obj)
            if defn:
                return ResolvedAgentSpec(
                    name=defn.name,
                    system_prompt_path=defn.source_path or path_obj,
                    system_prompt_args={},
                    model=None,
                    when_to_use=defn.description,
                    tools=list(defn.tools) if defn.tools is not None else ["read", "bash", "edit"],
                    allowed_tools=list(defn.tools) if defn.tools is not None else None,
                    exclude_tools=[],
                    subagents={},
                )

    # Check bundled agents (e.g. "default", "okabe")
    bundled_file = get_agents_dir() / str(name_or_path) / "agent.yaml"
    if bundled_file.is_file():
        return load_agent_spec(bundled_file)

    # Check discovered markdown agent specs (.coderai/agents/*.md, etc.)
    from coderai.subagents.registry import discover_markdown_agents

    discovered = discover_markdown_agents(project_root or Path.cwd())
    target_norm = str(name_or_path).strip().lower()
    for defn in discovered:
        if defn.name.lower() == target_norm or (
            defn.source_path and defn.source_path.stem.lower() == target_norm
        ):
            return ResolvedAgentSpec(
                name=defn.name,
                system_prompt_path=defn.source_path or Path.cwd(),
                system_prompt_args={},
                model=None,
                when_to_use=defn.description,
                tools=list(defn.tools) if defn.tools is not None else ["read", "bash", "edit"],
                allowed_tools=list(defn.tools) if defn.tools is not None else None,
                exclude_tools=[],
                subagents={},
            )

    raise AgentSpecError(f"Agent spec not found for name or path: {name_or_path}")
