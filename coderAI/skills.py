"""Skills discovery and loading for CoderAI."""

import logging
from pathlib import Path
from typing import Dict, List, Optional

import yaml


logger = logging.getLogger(__name__)


class Skill:
    """Represents a skill loaded from a markdown file."""

    def __init__(self, name: str, description: str, instructions: str):
        self.name = name
        self.description = description
        self.instructions = instructions


def _find_skills_dir(project_root: str = ".") -> Optional[Path]:
    """Search for the .coderAI/skills/ directory."""
    candidates = [
        Path(project_root).resolve(),
        Path.cwd(),
        Path(__file__).resolve().parent.parent,
    ]
    seen: set = set()
    for base in candidates:
        base_str = str(base)
        if base_str in seen:
            continue
        seen.add(base_str)
        skills_dir = base / ".coderAI" / "skills"
        if skills_dir.is_dir():
            return skills_dir
    return None


def load_skill(skill_name: str, project_root: str = ".") -> Optional[Skill]:
    """Load a skill from .coderAI/skills/<skill_name>.md.

    Parses YAML frontmatter for metadata (name, description)
    and uses the rest of the markdown as the skill instructions.
    """
    skills_dir = _find_skills_dir(project_root)
    if skills_dir is None:
        return None

    file_path = skills_dir / f"{skill_name}.md"
    if not file_path.exists():
        return None

    try:
        content = file_path.read_text()

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

        return Skill(
            name=metadata.get("name", skill_name),
            description=metadata.get("description", f"Skill: {skill_name}"),
            instructions=instructions,
        )
    except Exception as e:
        logger.error(f"Error loading skill {skill_name}: {e}")
        return None


def get_available_skills(project_root: str = ".") -> List[Dict[str, str]]:
    """Return a list of available skills with name and description."""
    skills_dir = _find_skills_dir(project_root)
    if skills_dir is None:
        return []

    skills = []
    for f in sorted(skills_dir.glob("*.md")):
        skill = load_skill(f.stem, project_root)
        if skill:
            skills.append({"name": skill.name, "description": skill.description})
    return skills
