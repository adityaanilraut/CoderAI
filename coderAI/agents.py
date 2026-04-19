import logging
import re
from pathlib import Path
from typing import Dict, Optional, List, Set
import yaml

from .config import config_manager

logger = logging.getLogger(__name__)


PERSONA_NAME_ALIASES: Dict[str, str] = {
    "code-reviewer": "code-reviewer",
    "code-review": "code-reviewer",
    "code-review-specialist": "code-reviewer",
    "security-reviewer": "security-reviewer",
    "security-review": "security-reviewer",
    "security-expert": "security-reviewer",
    "architect": "architect",
    "architecture": "architect",
    "planner": "planner",
    "plan": "planner",
    "doc-updater": "doc-updater",
    "documentation": "doc-updater",
    "documentation-updater": "doc-updater",
    "e2e": "e2e-runner",
    "e2e-runner": "e2e-runner",
}

PERSONA_TOOL_ALIASES: Dict[str, Set[str]] = {
    "read": {"read_file"},
    "write": {"write_file"},
    "edit": {"search_replace", "apply_diff"},
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
    """Search several candidate locations for the .coderAI/agents/ directory.
    
    Checks (in order):
      1. The given project_root
      2. The current working directory
      3. The CoderAI package source directory (parent of this file)
    
    Returns the first existing agents directory, or None.
    """
    candidates = [
        Path(project_root).resolve(),
        Path.cwd(),
        Path(__file__).resolve().parent.parent,  # repo root when running from source
    ]
    seen: set = set()
    for base in candidates:
        base_str = str(base)
        if base_str in seen:
            continue
        seen.add(base_str)
        agents_dir = base / ".coderAI" / "agents"
        if agents_dir.is_dir():
            return agents_dir
    return None


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
        
        # Map common aliases using the authoritative MODEL_ALIASES from anthropic.py
        if isinstance(model_name, str):
            try:
                from .llm.anthropic import MODEL_ALIASES as _anthropic_aliases
                model_name = _anthropic_aliases.get(model_name.lower(), model_name)
            except ImportError:
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
