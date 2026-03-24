import logging
from pathlib import Path
from typing import Dict, Optional, List
import yaml

from .config import config_manager

logger = logging.getLogger(__name__)

class AgentPersona:
    """Represents a specialized agent persona loaded from a markdown file."""
    
    def __init__(self, name: str, description: str, tools: List[str], model: str, instructions: str):
        self.name = name
        self.description = description
        self.tools = tools
        self.model = model
        self.instructions = instructions


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


def load_agent_persona(persona_name: str, project_root: str = ".") -> Optional[AgentPersona]:
    """Load an agent persona from .coderAI/agents/<persona_name>.md.
    
    Parses YAML frontmatter for metadata (name, description, tools, model) 
    and uses the rest of the markdown as the system instructions.
    """
    agents_dir = _find_agents_dir(project_root)
    if agents_dir is None:
        return None
        
    file_path = agents_dir / f"{persona_name}.md"
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
            name=metadata.get("name", persona_name),
            description=metadata.get("description", f"Specialized {persona_name} agent"),
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
