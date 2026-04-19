"""Skill usage tool for loading and applying skill instructions."""

import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .base import Tool
from ..skills import get_available_skills, load_skill
from ..config import config_manager

logger = logging.getLogger(__name__)


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
