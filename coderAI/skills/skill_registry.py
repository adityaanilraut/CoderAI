"""In-memory skill registry with session-level caching."""

from __future__ import annotations

import logging
from typing import Dict, Iterable, List, Optional

from coderAI.skills.skill_loader import Skill

logger = logging.getLogger(__name__)


class SkillRegistry:
    """Session-scoped container that indexes skills by name.

    Provides ``register`` / ``get`` / ``list_all`` / ``clear`` and
    automatically deduplicates by skill name (last-write wins).
    """

    def __init__(self) -> None:
        self._skills: Dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        """Add or update a skill in the registry.

        Existing skills with the same name are overwritten.
        """
        if skill.name in self._skills:
            logger.debug("[SkillRegistry] Overwriting existing skill: %s", skill.name)
        else:
            logger.debug("[SkillRegistry] Registered skill: %s", skill.name)
        self._skills[skill.name] = skill

    def register_all(self, skills: Iterable[Skill]) -> None:
        """Bulk-register a collection of skills."""
        for skill in skills:
            self.register(skill)

    def get(self, name: str) -> Optional[Skill]:
        """Retrieve a skill by its unique name."""
        return self._skills.get(name)

    def list_all(self) -> List[Skill]:
        """Return every registered skill in insertion order."""
        return list(self._skills.values())

    def find_by_source(self, source: str) -> List[Skill]:
        """Filter skills that originate from a given source label."""
        return [s for s in self._skills.values() if s.source == source]

    def clear(self) -> None:
        """Drop all registered skills (useful when resetting a session)."""
        self._skills.clear()
        logger.debug("[SkillRegistry] Registry cleared")

    def __len__(self) -> int:
        return len(self._skills)

    def __contains__(self, name: str) -> bool:
        return name in self._skills
