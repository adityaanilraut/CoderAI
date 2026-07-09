"""Local skill source — scans ``.coderAI/skills/`` for ``SKILLS.md`` files."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from coderAI.skills.skill_manager import Skill, discover_local_skills, load_skill_by_name
from coderAI.skills.sources.base import SkillSource

logger = logging.getLogger(__name__)


class LocalSkillSource(SkillSource):
    """Discovers skills stored as ``.coderAI/skills/<name>/SKILLS.md``."""

    def __init__(self, project_root: str = ".") -> None:
        self._project_root = str(Path(project_root).resolve())

    @property
    def source_name(self) -> str:
        return "local"

    async def discover(self) -> List[Skill]:
        """Scan the local skills directory."""
        return discover_local_skills(self._project_root)

    async def get_skill(self, name: str) -> Optional[Skill]:
        """Retrieve a single local skill by name."""
        return load_skill_by_name(name, self._project_root)
