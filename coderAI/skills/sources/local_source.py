"""Local skill source — scans ``.coderAI/skills/`` for ``SKILLS.md`` files."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional, Tuple

from coderAI.skills.skill_loader import Skill, discover_local_skills, load_skill_by_name
from coderAI.skills.sources.base import SkillSource

logger = logging.getLogger(__name__)


class LocalSkillSource(SkillSource):
    """Discovers skills stored in the project's ``.coderAI/skills/`` directory.

    Supports both the subdirectory format (``skills/<name>/SKILLS.md``) and
    legacy flat files (``skills/<name>.md``).
    """

    def __init__(self, project_root: str = ".") -> None:
        self._project_root = str(Path(project_root).resolve())

    @property
    def source_name(self) -> str:
        return "local"

    async def discover(self) -> List[Skill]:
        """Scan the local skills directory."""
        return discover_local_skills(self._project_root)

    async def search(
        self, query: str, top_n: int = 5
    ) -> List[Tuple[Skill, float]]:
        """Simple keyword overlap search for local skills.

        NOTE: Full semantic matching is handled by ``SkillManager`` which
        uses the LLM provider. This method provides a fast baseline for
        cases where an LLM call is not desired.
        """
        skills = await self.discover()
        if not skills:
            return []

        query_lower = query.lower()
        scored: List[Tuple[Skill, float]] = []

        for skill in skills:
            score = 0.0
            searchable = f"{skill.name} {skill.description} {' '.join(skill.tags)}".lower()

            # Word-level overlap bonus
            query_words = set(query_lower.split())
            text_words = set(searchable.split())
            overlap = query_words & text_words
            if overlap:
                score = len(overlap) / max(len(query_words), 1)
                score = min(score, 0.6)  # cap keyword-only confidence

            if score > 0.0:
                scored.append((skill, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_n]

    async def get_skill(self, name: str) -> Optional[Skill]:
        """Retrieve a single local skill by name."""
        return load_skill_by_name(name, self._project_root)
