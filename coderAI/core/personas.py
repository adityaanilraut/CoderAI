import re
import yaml
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Any, Literal

from coderAI.assets.manifest import asset_directory
from coderAI.system.config import config_manager

logger = logging.getLogger(__name__)

# Aliases for personas whose friendly name differs from the filename stem.
# Add entries here only when the mapping actually renames — identity pairs
# like "planner" → "planner" are handled by the default normalized lookup.
PERSONA_NAME_ALIASES = {
    "reviewer": "code-reviewer",
}

# Mapping of persona tool labels to registry tool names
PERSONA_TOOL_ALIASES: dict[str, set[str]] = {
    "read": {"read_file", "list_directory", "glob_search"},
    "write": {"write_file", "search_replace", "apply_diff"},
    "edit": {"search_replace", "apply_diff"},
    "search": {"grep", "glob_search"},
    "grep": {"grep"},
    "glob": {"glob_search"},
    "bash": {"run_command", "run_background", "python_repl"},
}

PersonaScope = Literal["project", "user", "builtin"]


@dataclass(frozen=True)
class PersonaDescriptor:
    """One discoverable persona and the scope that supplied it."""

    name: str
    scope: PersonaScope
    path: Path


class AgentPersona:
    """Represents a specialized agent persona loaded from a markdown file."""

    def __init__(
        self,
        name: str,
        description: str,
        tools: list[str],
        model: str,
        instructions: str,
        mode: str = "all",
        hidden: bool = False,
        permission: Optional[dict[str, str]] = None,
        source: PersonaScope = "builtin",
    ):
        self.name = name
        self.description = description
        self.tools = tools
        self.model = model
        self.instructions = instructions
        # Agent mode: "primary" (main agent only), "subagent" (delegation only),
        # "all" (usable anywhere), "hidden" (internal system agents)
        self.mode: str = mode
        self.hidden = hidden
        # Per-agent permission rules: {"tool_name": "allow"|"deny"}
        self.permission: dict[str, str] = permission or {}
        self.source = source


def persona_allowed_in_context(persona: "AgentPersona", *, is_subagent: bool) -> bool:
    """Return whether ``persona`` may run in the given launch context (Phase 5.3).

    Enforces the persona ``mode``/``hidden`` frontmatter so an agent tree stays
    well-formed and a repo-supplied persona can't be launched where it doesn't
    belong:

    * A ``subagent`` or ``hidden`` persona can never be the **primary** agent.
    * A ``primary`` persona can never be launched as a delegated **sub-agent**.

    ``mode == "all"`` (the default) is allowed anywhere.
    """
    mode = (getattr(persona, "mode", None) or "all").strip().lower()
    if is_subagent:
        return mode != "primary"
    # Primary / root launch context.
    return not (bool(getattr(persona, "hidden", False)) or mode in ("subagent", "hidden"))


def _normalize_persona_name(name: str) -> str:
    """Normalize user-facing persona names to a filename-friendly key."""
    return re.sub(r"[-_\s]+", "-", name.strip().lower())


def _normalize_tool_name(name: str) -> str:
    """Normalize persona tool labels to registry-style tool names."""
    return re.sub(r"[-\s]+", "_", name.strip().lower())


def _find_agents_dir(project_root: str = ".") -> Optional[Path]:
    """Return the exact project persona directory without checkout fallbacks."""
    root = Path(project_root).expanduser().resolve()
    path = root / ".coderAI" / "agents"
    try:
        resolved = path.resolve()
        return resolved if resolved.is_dir() and resolved.is_relative_to(root) else None
    except OSError:
        return None


def _user_agents_dir() -> Optional[Path]:
    root = Path(config_manager.config_dir).expanduser().resolve()
    path = root / "agents"
    try:
        resolved = path.resolve()
        return resolved if resolved.is_dir() and resolved.is_relative_to(root) else None
    except OSError:
        return None


def _persona_roots(
    project_root: str,
    *,
    include_project: bool,
    include_user: bool,
    include_builtin: bool,
) -> list[tuple[PersonaScope, Path]]:
    roots: list[tuple[PersonaScope, Path]] = []
    if include_project:
        project = _find_agents_dir(project_root)
        if project is not None:
            roots.append(("project", project))
    if include_user:
        user = _user_agents_dir()
        if user is not None:
            roots.append(("user", user))
    if include_builtin:
        try:
            roots.append(("builtin", asset_directory("agents")))
        except FileNotFoundError:
            logger.error("Built-in persona package resources are unavailable")
    return roots


def get_available_persona_descriptors(
    project_root: str = ".",
    *,
    include_project: bool = True,
    include_user: bool = True,
    include_builtin: bool = True,
) -> list[PersonaDescriptor]:
    """Return personas in precedence order: project, user, then built-in."""
    descriptors: list[PersonaDescriptor] = []
    seen: set[str] = set()
    for scope, root in _persona_roots(
        project_root,
        include_project=include_project,
        include_user=include_user,
        include_builtin=include_builtin,
    ):
        try:
            files = sorted(root.glob("*.md"))
        except OSError:
            logger.warning("Could not list %s persona directory %s", scope, root)
            continue
        for path in files:
            normalized = _normalize_persona_name(path.stem)
            if normalized in seen or not path.is_file():
                continue
            try:
                if not path.resolve().is_relative_to(root.resolve()):
                    continue
            except OSError:
                continue
            descriptors.append(PersonaDescriptor(path.stem, scope, path))
            seen.add(normalized)
    return descriptors


def _safe_persona_stem(name: str) -> str:
    """Return a plain persona stem, rejecting path syntax and traversal."""
    candidate = name.strip()
    if not candidate or ".." in candidate or "/" in candidate or "\\" in candidate:
        return ""
    return candidate


def resolve_persona_name(
    persona_name: str,
    project_root: str = ".",
    *,
    include_project: bool = True,
    include_user: bool = True,
    include_builtin: bool = True,
) -> Optional[str]:
    """Resolve flexible persona names to an existing persona file stem."""
    if not persona_name:
        return None

    candidate = _safe_persona_stem(persona_name.strip())
    if not candidate:
        return None

    normalized = _normalize_persona_name(candidate)
    aliased = PERSONA_NAME_ALIASES.get(normalized, normalized)

    for descriptor in get_available_persona_descriptors(
        project_root,
        include_project=include_project,
        include_user=include_user,
        include_builtin=include_builtin,
    ):
        if descriptor.name == candidate or _normalize_persona_name(descriptor.name) == aliased:
            return descriptor.name

    return None


def expand_persona_tools(tool_names: list[str]) -> set[str]:
    """Expand persona tool labels into concrete registry tool names."""
    expanded: set[str] = set()
    for tool_name in tool_names or []:
        normalized = _normalize_tool_name(tool_name)
        expanded.add(normalized)
        expanded.update(PERSONA_TOOL_ALIASES.get(normalized, set()))
    return expanded


def load_agent_persona(
    persona_name: str,
    project_root: str = ".",
    *,
    include_project: bool = True,
    include_user: bool = True,
    include_builtin: bool = True,
) -> Optional[AgentPersona]:
    """Load a persona using project → user → built-in precedence.

    Parses YAML frontmatter for metadata (name, description, tools, model)
    and uses the rest of the markdown as the system instructions.
    """
    resolved_name = resolve_persona_name(
        persona_name,
        project_root,
        include_project=include_project,
        include_user=include_user,
        include_builtin=include_builtin,
    ) or _safe_persona_stem(persona_name)
    descriptor = next(
        (
            item
            for item in get_available_persona_descriptors(
                project_root,
                include_project=include_project,
                include_user=include_user,
                include_builtin=include_builtin,
            )
            if item.name == resolved_name
        ),
        None,
    )
    if descriptor is None:
        return None
    try:
        root = descriptor.path.parent.resolve()
        file_path = descriptor.path.resolve()
        if not file_path.is_relative_to(root) or not file_path.is_file():
            return None
    except OSError:
        return None

    try:
        content = file_path.read_text()

        # Parse YAML frontmatter
        metadata: dict[str, Any] = {}
        instructions = content

        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    metadata = yaml.safe_load(parts[1]) or {}
                    instructions = parts[2].strip()
                except yaml.YAMLError as e:
                    logger.warning(f"Failed to parse YAML frontmatter in {file_path.name}: {e}")
        model_name = metadata.get("model", config_manager.load().default_model)

        # Resolve friendly aliases via the factory (the single alias seam), so
        # core never imports a specific provider module. Degrade gracefully to
        # the unmapped name if the factory can't be imported.
        if isinstance(model_name, str):
            try:
                from coderAI.llm.factory import resolve_model_alias

                model_name = resolve_model_alias(model_name)
            except (ImportError, AttributeError):
                pass

        return AgentPersona(
            name=metadata.get("name", resolved_name),
            description=metadata.get("description", f"Specialized {resolved_name} agent"),
            tools=metadata.get("tools", []),
            model=model_name,
            instructions=instructions,
            mode=metadata.get("mode", "all"),
            hidden=metadata.get("hidden", False),
            permission=metadata.get("permission"),
            source=descriptor.scope,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as e:
        logger.error(f"Error loading agent persona {persona_name}: {e}")
        return None


def get_available_personas(
    project_root: str = ".",
    *,
    include_project: bool = True,
    include_user: bool = True,
    include_builtin: bool = True,
) -> list[str]:
    """Return unique persona names in project → user → built-in precedence."""
    return [
        item.name
        for item in get_available_persona_descriptors(
            project_root,
            include_project=include_project,
            include_user=include_user,
            include_builtin=include_builtin,
        )
    ]
