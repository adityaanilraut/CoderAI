import re
import yaml
import logging
from pathlib import Path
from typing import List, Optional, Set, Dict

from .config import config_manager
from .project_layout import find_dot_coderai_subdir

logger = logging.getLogger(__name__)

# Aliases for personas whose friendly name differs from the filename stem.
# Add entries here only when the mapping actually renames — identity pairs
# like "planner" → "planner" are handled by the default normalized lookup.
PERSONA_NAME_ALIASES = {
    "reviewer": "code-reviewer",
}

# Mapping of persona tool labels to registry tool names
PERSONA_TOOL_ALIASES: Dict[str, Set[str]] = {
    "read": {"read_file", "list_directory", "glob_search"},
    "write": {"write_file", "search_replace", "apply_diff"},
    "edit": {"search_replace", "apply_diff"},
    "search": {"grep", "glob_search", "text_search"},
    "grep": {"grep", "text_search"},
    "glob": {"glob_search"},
    "bash": {"run_command", "run_background", "python_repl"},
}


class AgentPersona:
    """Represents a specialized agent persona loaded from a markdown file."""

    def __init__(self, name: str, description: str, tools: List[str], model: str, instructions: str):
        self.name = name
        self.description = description
        self.tools = tools
        self.model = model
        self.instructions = instructions


def _normalize_persona_name(name: str) -> str:
    """Normalize user-facing persona names to a filename-friendly key."""
    return re.sub(r"[-_\s]+", "-", name.strip().lower())


def _normalize_tool_name(name: str) -> str:
    """Normalize persona tool labels to registry-style tool names."""
    return re.sub(r"[-\s]+", "_", name.strip().lower())


def _find_agents_dir(project_root: str = ".") -> Optional[Path]:
    """Search several candidate locations for the .coderAI/agents/ directory."""
    return find_dot_coderai_subdir("agents", project_root)


def resolve_persona_name(persona_name: str, project_root: str = ".") -> Optional[str]:
    """Resolve flexible persona names to an existing persona file stem."""
    agents_dir = _find_agents_dir(project_root)
    if agents_dir is None or not persona_name:
        return None

    candidate = persona_name.strip()
    if not candidate:
        return None

    if (agents_dir / f"{candidate}.md").exists():
        return candidate

    normalized = _normalize_persona_name(candidate)
    aliased = PERSONA_NAME_ALIASES.get(normalized, normalized)

    for stem in get_available_personas(project_root):
        if stem == aliased or _normalize_persona_name(stem) == aliased:
            return stem

    return None


def expand_persona_tools(tool_names: List[str]) -> Set[str]:
    """Expand persona tool labels into concrete registry tool names."""
    expanded: Set[str] = set()
    for tool_name in tool_names or []:
        normalized = _normalize_tool_name(tool_name)
        expanded.add(normalized)
        expanded.update(PERSONA_TOOL_ALIASES.get(normalized, set()))
    return expanded


def load_agent_persona(persona_name: str, project_root: str = ".") -> Optional[AgentPersona]:
    """Load an agent persona from .coderAI/agents/<persona_name>.md.

    Parses YAML frontmatter for metadata (name, description, tools, model)
    and uses the rest of the markdown as the system instructions.
    """
    agents_dir = _find_agents_dir(project_root)
    if agents_dir is None:
        return None

    resolved_name = resolve_persona_name(persona_name, project_root) or persona_name
    file_path = agents_dir / f"{resolved_name}.md"
    if not file_path.exists():
        return None

    try:
        content = file_path.read_text()

        # Parse YAML frontmatter
        metadata = {}
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

        # Map common aliases using the authoritative MODEL_ALIASES from anthropic.py.
        # Tolerate both a missing module (optional dep) and a restructured
        # module (attribute rename); both should degrade gracefully to the
        # unmapped name.
        if isinstance(model_name, str):
            try:
                from .llm.anthropic import MODEL_ALIASES as _anthropic_aliases
                model_name = _anthropic_aliases.get(model_name.lower(), model_name)
            except (ImportError, AttributeError):
                pass

        return AgentPersona(
            name=metadata.get("name", resolved_name),
            description=metadata.get("description", f"Specialized {resolved_name} agent"),
            tools=metadata.get("tools", []),
            model=model_name,
            instructions=instructions
        )
    except Exception as e:
        logger.error(f"Error loading agent persona {persona_name}: {e}")
        return None


def get_available_personas(project_root: str = ".") -> List[str]:
    """Return a list of available persona names."""
    agents_dir = _find_agents_dir(project_root)
    if agents_dir is None:
        return []

    return [f.stem for f in agents_dir.glob("*.md")]
