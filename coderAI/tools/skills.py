"""Skill usage tool for loading and applying skill instructions."""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field

from .base import Tool
from ..config import config_manager
from ..project_layout import find_dot_coderai_subdir

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Skill loader (inlined from the former coderAI/skills.py)
# ---------------------------------------------------------------------------

class Skill:
    """Represents a skill loaded from a markdown file."""

    def __init__(self, name: str, description: str, instructions: str):
        self.name = name
        self.description = description
        self.instructions = instructions


def _find_skills_dir(project_root: str = ".") -> Optional[Path]:
    """Search for the .coderAI/skills/ directory."""
    return find_dot_coderai_subdir("skills", project_root)


def load_skill(skill_name: str, project_root: str = ".") -> Optional[Skill]:
    """Load a skill from .coderAI/skills/<skill_name>.md."""
    skills_dir = _find_skills_dir(project_root)
    if skills_dir is None:
        return None

    file_path = skills_dir / f"{skill_name}.md"
    if not file_path.exists():
        return None

    try:
        content = file_path.read_text()
        metadata: Dict[str, Any] = {}
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



class UseSkillParams(BaseModel):
    action: str = Field(
        ...,
        description="Action to perform: 'list' to see available skills, or 'use' to load a skill's instructions.",
    )
    skill_name: Optional[str] = Field(
        None,
        description="Name of the skill to load (required for 'use' action). Example: 'security-audit'.",
    )


class UseSkillTool(Tool):
    """Tool for discovering and loading skill instructions."""

    name = "use_skill"
    description = (
        "Load a predefined skill workflow from the project's .coderAI/skills/ directory. "
        "Skills provide step-by-step instructions for common workflows like security audits, "
        "TDD, testing, etc. Use action='list' to see available skills, then action='use' "
        "with skill_name to load the full instructions."
    )
    parameters_model = UseSkillParams
    is_read_only = True

    async def execute(
        self,
        action: str,
        skill_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute skill action."""
        try:
            config = config_manager.load_project_config(".")
            project_root = config.project_root

            if action == "list":
                skills = get_available_skills(project_root)
                if not skills:
                    return {
                        "success": True,
                        "message": "No skills found in .coderAI/skills/ directory.",
                        "skills": [],
                        "hint": "Create .md files in .coderAI/skills/ with YAML frontmatter to define skills.",
                    }
                return {
                    "success": True,
                    "skills": skills,
                    "count": len(skills),
                    "hint": "Use action='use' with skill_name to load a skill's instructions.",
                }

            elif action == "use":
                if not skill_name:
                    return {
                        "success": False,
                        "error": "skill_name is required for 'use' action.",
                    }

                skill = load_skill(skill_name, project_root)
                if skill is None:
                    available = get_available_skills(project_root)
                    return {
                        "success": False,
                        "error": f"Skill '{skill_name}' not found.",
                        "available_skills": [s["name"] for s in available],
                    }

                return {
                    "success": True,
                    "skill_name": skill.name,
                    "description": skill.description,
                    "instructions": skill.instructions,
                    "note": "Follow the instructions above to complete the skill workflow.",
                }

            else:
                return {"success": False, "error": f"Unknown action: {action}. Use 'list' or 'use'."}

        except Exception as e:
            return {"success": False, "error": str(e)}
