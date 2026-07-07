"""Skill usage tool for loading and applying skill instructions.

Now delegates to the centralized ``coderAI.skills`` package for skill
discovery, loading, and relevance matching.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from coderAI.tools.base import Tool
from coderAI.system.config import config_manager
from coderAI.skills import (
    Skill,
    discover_local_skills,
    load_skill_by_name,
)

logger = logging.getLogger(__name__)

__all__ = [
    "Skill",
    "load_skill",
    "get_available_skills",
    "UseSkillParams",
    "UseSkillTool",
]


def load_skill(skill_name: str, project_root: str = ".") -> Optional[Skill]:
    """Load a single skill from ``.coderAI/skills/``.

    Prefer the subdirectory format (``skills/<name>/SKILLS.md``) but fall
    back to the legacy flat file (``skills/<name>.md``).

    Maintained for backward compatibility; delegates to
    :func:`coderAI.skills.load_skill_by_name`.
    """
    return load_skill_by_name(skill_name, project_root)


def get_available_skills(project_root: str = ".") -> List[Dict[str, str]]:
    """Return a list of available skills with name and description.

    Maintained for backward compatibility; delegates to
    :func:`coderAI.skills.discover_local_skills`.
    """
    skills = discover_local_skills(project_root)
    return [{"name": s.name, "description": s.description} for s in skills]


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
    category = "skills"
    parameters_model = UseSkillParams
    is_read_only = True

    async def execute(  # type: ignore[override]
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
                        "hint": (
                            "Create SKILLS.md files in .coderAI/skills/<name>/ "
                            "with YAML frontmatter to define skills."
                        ),
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
                return {
                    "success": False,
                    "error": f"Unknown action: {action}. Use 'list' or 'use'.",
                }

        except Exception as e:
            return {"success": False, "error": str(e)}
