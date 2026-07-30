"""Local skill source — scans project and/or user skill directories."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from coderAI.skills.skill_manager import Skill, discover_local_skills, load_skill_by_name
from coderAI.skills.sources.base import SkillSource

logger = logging.getLogger(__name__)


class LocalSkillSource(SkillSource):
    """Discovers skills from ``.coderAI/skills/`` and optionally ``~/.coderAI/skills/``.

    Accepts both canonical ``SKILLS.md`` and ecosystem ``SKILL.md`` filenames.
    """

    def __init__(
        self,
        project_root: str = ".",
        *,
        include_project: bool = True,
        include_user: bool = True,
    ) -> None:
        self._project_root = str(Path(project_root).resolve())
        self._include_project = include_project
        self._include_user = include_user

    @property
    def source_name(self) -> str:
        parts: list[str] = []
        if self._include_project:
            parts.append("project")
        if self._include_user:
            parts.append("user")
        return "local:" + "+".join(parts) if parts else "local"

    async def discover(self) -> list[Skill]:
        """Scan configured skill directories."""
        return discover_local_skills(
            self._project_root,
            include_project=self._include_project,
            include_user=self._include_user,
        )

    async def get_skill(self, name: str) -> Optional[Skill]:
        """Retrieve a single skill by name."""
        return load_skill_by_name(
            name,
            self._project_root,
            include_project=self._include_project,
            include_user=self._include_user,
        )
